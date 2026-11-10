"""QA review findings on design/windows-service: operator-readable status, writable code paths,
visible startup failures, stop deadline / shutdown budget."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import make_config
from sentinel.db import Ledger
from sentinel.platform import winacl
from sentinel.status import fatal_status_document, read_status, write_status
from sentinel.winsvc import codeacl, scm
from sentinel.winsvc.__main__ import code_write_problems, run_recorder_in_host

ME = "S-1-5-21-3365957325-2048722254-1525407219-1001"
TI = codeacl.SID_TRUSTED_INSTALLER
# Captured on the dev host: C:\Program Files (owner TrustedInstaller; Users read+execute only).
SDDL_PROGRAM_FILES = (f"O:{TI}D:P(A;;FA;;;{TI})(A;CIIO;GA;;;{TI})(A;;0x1301bf;;;SY)(A;OICIIO;GA;;;SY)"
                      "(A;;0x1301bf;;;BA)(A;OICIIO;GA;;;BA)(A;;0x1200a9;;;BU)(A;OICIIO;GXGR;;;BU)(A;OICIIO;GA;;;CO)"
                      "(A;;0x1200a9;;;AC)(A;OICIIO;GXGR;;;AC)(A;;0x1200a9;;;S-1-15-2-2)(A;OICIIO;GXGR;;;S-1-15-2-2)")
# Generated fixture: the default ACL Windows gives a folder created directly under C:\ (e.g. C:\Sentinel):
# Authenticated Users get Modify (0x1301bf) — anyone who can log on can replace the code.
SDDL_C_ROOT_CHILD = ("O:BAD:AI(A;OICIID;FA;;;BA)(A;OICIID;FA;;;SY)(A;OICIID;0x1200a9;;;BU)"
                     "(A;ID;0x1301bf;;;AU)(A;OICIIOID;SDGXGWGR;;;AU)")
# Captured: per-user Python install (owner and full control: the user).
SDDL_USER_PYTHON = f"O:{ME}D:AI(A;ID;FA;;;SY)(A;ID;FA;;;BA)(A;ID;FA;;;{ME})"


# ------------------------------------------------------------------ 2.3 writable code
def test_program_files_acl_is_not_writable_by_non_admins() -> None:
    assert codeacl.write_problems(winacl.parse_sddl(SDDL_PROGRAM_FILES)) == []


def test_folder_under_c_root_is_writable_by_authenticated_users() -> None:
    probs = codeacl.write_problems(winacl.parse_sddl(SDDL_C_ROOT_CHILD))
    assert len(probs) == 1 and winacl.SID_AUTHENTICATED_USERS in probs[0]


def test_user_owned_python_is_rejected_for_owner_and_grant() -> None:
    probs = codeacl.write_problems(winacl.parse_sddl(SDDL_USER_PYTHON))
    assert any("owned by" in p for p in probs) and any(ME in p and "grants" in p for p in probs)


def test_read_only_grants_to_users_are_fine() -> None:
    assert codeacl.write_problems(winacl.parse_sddl("O:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FRFX;;;BU)")) == []
    assert codeacl.write_problems(winacl.parse_sddl("O:BAD:P(A;;FA;;;BA)(A;;0x40;;;BU)"))   # delete child


def test_scan_walks_trees_including_pycache_and_caches(tmp_path: Path) -> None:
    (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / "pkg" / "mod.py").write_text("")
    planted = tmp_path / "pkg" / "__pycache__" / "mod.cpython-312.pyc"
    planted.write_bytes(b"")
    calls: list[Path] = []

    def fake(p: Path) -> str:
        calls.append(p)
        return SDDL_C_ROOT_CHILD if p == planted else SDDL_PROGRAM_FILES
    probs = codeacl.scan([], [tmp_path / "pkg"], read_sddl=fake)
    assert len(probs) == 1 and str(planted) in probs[0]
    assert planted in calls and len(calls) == 4


def test_code_write_problems_covers_interpreter_pythonpath_tools_and_config(tmp_path: Path) -> None:
    from types import SimpleNamespace

    tools = tmp_path / "ffmpeg" / "bin"
    tools.mkdir(parents=True)
    for n in ("ffmpeg.exe", "ffprobe.exe"):
        (tools / n).write_bytes(b"")
    py = tmp_path / "py"
    py.mkdir()
    (py / "python.exe").write_bytes(b"")
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "sentinel.toml").write_text("")
    cfg = SimpleNamespace(tools=SimpleNamespace(ffmpeg=str(tools / "ffmpeg.exe"), ffprobe=str(tools / "ffprobe.exe")))
    host = SimpleNamespace(interpreter=str(py / "python.exe"), read_paths=(str(py), str(tmp_path / "src")))
    seen: dict[str, list] = {}

    def fake_scan(singles, roots):  # noqa: ANN001, ANN202
        seen["singles"], seen["roots"] = list(singles), list(roots)
        return []
    code_write_problems(cfg, cfg_dir / "sentinel.toml", host, scan=fake_scan)
    assert set(seen["singles"]) == {py / "python.exe", cfg_dir / "sentinel.toml", cfg_dir,
                                    tools / "ffmpeg.exe", tools / "ffprobe.exe"}
    assert set(seen["roots"]) == {py, tools}          # src does not exist here: skipped
    code_write_problems(cfg, cfg_dir / "sentinel.toml", host, recursive=False, scan=fake_scan)
    assert seen["roots"] == [] and py in seen["singles"]


@pytest.mark.skipif(sys.platform != "win32", reason="real DACLs")
def test_real_scan_flags_a_user_writable_tree(tmp_path: Path) -> None:
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "x.py").write_text("")
    probs = codeacl.scan([], [tmp_path], limit=50)
    assert probs and all("grants" in p or "owned by" in p for p in probs)


# ------------------------------------------------------------------ 2.1 operator-readable status
def test_status_dir_and_snapshot_paths(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    assert cfg.status_path == cfg.data_path / "status.json" and cfg.ledger_snapshot_path is None
    cfg2 = make_config(tmp_path, storage={"status_dir": str(tmp_path / "status")})
    assert cfg2.status_path == tmp_path / "status" / "status.json"
    assert cfg2.ledger_snapshot_path == tmp_path / "status" / "ledger-snapshot.db"


def test_ledger_snapshot_is_a_self_contained_read_only_copy(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "data" / "sentinel.db")
    try:
        led.add_event("info", "recorder", "x1", "first")
        snap = tmp_path / "status" / "ledger-snapshot.db"
        led.write_snapshot(snap)
        led.add_event("info", "recorder", "x2", "second")
        led.write_snapshot(snap)                      # atomic replace of the previous copy
    finally:
        led.close()
    assert not (tmp_path / "status" / "ledger-snapshot.db-wal").exists()
    conn = sqlite3.connect(f"{snap.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        codes = [r[0] for r in conn.execute("SELECT code FROM events WHERE code LIKE 'x%' ORDER BY event_id")]
        assert codes == ["x1", "x2"]
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()
    assert not list((tmp_path / "status").glob(".*.tmp"))


# ------------------------------------------------------------------ 2.4 visible startup failure
def test_terminal_status_is_shown_even_when_old_but_ordinary_status_goes_stale(tmp_path: Path) -> None:
    p = tmp_path / "status.json"
    doc = fatal_status_document("C-001", "CAM01")
    doc.heartbeat_epoch = time.time() - 3600
    write_status(p, doc)
    got, problem = read_status(p)
    assert problem is None and got is not None and got["alert"]["code"] == "C-001"
    assert got["alert"]["message"].startswith("Recording stopped. Error C-001")
    doc.terminal = False
    write_status(p, doc)
    assert "X-001" in (read_status(p)[1] or "")


def test_recorder_startup_problem_writes_terminal_c001(tmp_path: Path) -> None:
    from sentinel.service import EXIT_CONFIG, RecorderService

    cfg = make_config(tmp_path, tools={"ffmpeg": "no-such-ffmpeg-xyz"})
    assert RecorderService(cfg).run() == EXIT_CONFIG
    st = json.loads(cfg.status_path.read_text("utf-8"))
    assert st["terminal"] is True and st["alert"]["code"] == "C-001"


class _Host:
    def __init__(self) -> None:
        self.system_shutdown = False
        self.stops: list = []
        self.running = False

    def on_stop(self, cb) -> None:  # noqa: ANN001
        self.stops.append(cb)

    def report_running(self) -> None:
        self.running = True


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


def test_config_error_in_service_is_logged_evented_and_shown_as_c001(tmp_path: Path) -> None:
    cfg = tmp_path / "sentinel.toml"
    cfg.write_text('[camera]\nid = "CAM07"\nurl = "http://not-allowed"\n'
                   '[storage]\nspool_dir = "spool"\ndata_dir = "data"\nlog_dir = "logs"\nstatus_dir = "status"\n',
                   encoding="utf-8")
    events: list[str] = []
    restore = _restore_logging()
    try:
        rc = run_recorder_in_host(_Host(), str(cfg), event_log=events.append, code_check=lambda c, p: [])
    finally:
        restore()
    assert rc == 2
    assert len(events) == 1 and "configuration error" in events[0] and "url scheme" in events[0]
    st = json.loads((tmp_path / "status" / "status.json").read_text("utf-8"))
    assert st["terminal"] and st["alert"]["code"] == "C-001" and st["camera_id"] == "CAM07"
    assert "url scheme" in (tmp_path / "logs" / "recorder.log").read_text("utf-8")


def test_unparseable_config_still_reaches_the_event_log(tmp_path: Path) -> None:
    cfg = tmp_path / "sentinel.toml"
    cfg.write_text("this is [not toml", encoding="utf-8")
    events: list[str] = []
    assert run_recorder_in_host(_Host(), str(cfg), event_log=events.append) == 2
    assert events and "not valid TOML" in events[0]


def test_service_refuses_to_start_from_writable_code(tmp_path: Path) -> None:
    from conftest import base_config_dict

    d = base_config_dict(tmp_path)
    d["storage"]["status_dir"] = str(tmp_path / "status")
    lines = []
    for sec, vals in d.items():
        lines.append(f"[{sec}]")
        lines += [f"{k} = {json.dumps(v)}" for k, v in vals.items()]
    cfg = tmp_path / "sentinel.toml"
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    events: list[str] = []
    made: list = []
    host = _Host()
    restore = _restore_logging()
    try:
        rc = run_recorder_in_host(host, str(cfg), recorder_factory=lambda c: made.append(c),
                                  event_log=events.append, code_check=lambda c, p: [r"C:\Sentinel grants ... AU"])
    finally:
        restore()
    assert rc == 2 and made == [] and not host.running
    st = json.loads((tmp_path / "status" / "status.json").read_text("utf-8"))
    assert st["alert"]["code"] == "C-001" and st["terminal"]
    assert "non-administrators" in events[0]


def test_host_routes_system_shutdown_to_bounded_stop(tmp_path: Path) -> None:
    from conftest import base_config_dict

    d = base_config_dict(tmp_path)
    cfg = tmp_path / "sentinel.toml"
    cfg.write_text("\n".join(f"[{s}]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in vals.items())
                             for s, vals in d.items()) + "\n", encoding="utf-8")

    class Svc:
        calls: list[str] = []

        def __init__(self, _cfg) -> None:  # noqa: ANN001
            pass

        def request_stop(self) -> None:
            self.calls.append("stop")

        def request_shutdown(self) -> None:
            self.calls.append("shutdown")

        def run(self) -> int:
            return 0
    host = _Host()
    restore = _restore_logging()
    try:
        assert run_recorder_in_host(host, str(cfg), recorder_factory=Svc, code_check=lambda c, p: []) == 0
    finally:
        restore()
    host.stops[0]()
    host.system_shutdown = True
    host.stops[0]()
    assert Svc.calls == ["stop", "shutdown"]


# ------------------------------------------------------------------ 2.5 stop deadline
class _FakeScm:
    def __init__(self) -> None:
        self.statuses: list[scm.ServiceStatus] = []
        self.handler = None

    def run_dispatcher(self, name, service_main) -> None:  # noqa: ANN001
        t = threading.Thread(target=service_main)
        t.start()
        t.join(30)

    def register_handler(self, name, handler):  # noqa: ANN001, ANN201
        self.handler = handler
        return 1

    def set_status(self, handle, status) -> None:  # noqa: ANN001
        self.statuses.append(status)


def test_stop_deadline_reports_stopped_before_ending_the_process() -> None:
    api = _FakeScm()
    order: list[str] = []
    release = threading.Event()

    def run(host: scm.ServiceHost) -> int:
        host.report_running()
        api.handler(scm.SERVICE_CONTROL_STOP, 0)
        release.wait(5)
        return 0

    def exit_fn(code: int) -> None:
        order.append(f"exit {code} after {scm.STATE_NAMES[api.statuses[-1].state]}"
                     f"/{api.statuses[-1].win32_exit_code}")
        release.set()
    scm.ServiceHost("S", run, api, stop_deadline_s=0.3, checkpoint_interval_s=0.05, exit_fn=exit_fn).dispatch()
    assert order == ["exit 6 after STOPPED/0"]          # STOPPED with no error: SCM will not restart it


def test_preshutdown_sets_system_shutdown_flag() -> None:
    host = scm.ServiceHost("S", lambda h: 0, _FakeScm())
    host.handle_control(scm.SERVICE_CONTROL_PRESHUTDOWN)
    assert host.system_shutdown and host.stop_requested.is_set()
    other = scm.ServiceHost("S", lambda h: 0, _FakeScm())
    other.handle_control(scm.SERVICE_CONTROL_STOP)
    assert not other.system_shutdown


@pytest.mark.skipif(sys.platform != "win32", reason="real icacls")
def test_status_folder_acl_gives_readers_read_but_not_write(tmp_path: Path) -> None:
    """Executes the generated status-folder icacls command (current user as the service, BUILTIN\\Users
    as the reader group — creating the real local group needs admin)."""
    from sentinel.winsvc.__main__ import _status_dir_report
    from sentinel.winsvc.commands import restrict_dir_steps

    me = winacl.current_user_sid()
    d = tmp_path / "status"
    d.mkdir()
    step = restrict_dir_steps(d, me, "M", extra_grants=("*S-1-5-32-545:(OI)(CI)RX",))[0]
    cp = subprocess.run(list(step.argv), capture_output=True, text=True)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    (d / "status.json").write_text("{}")
    dacl = winacl.read_dacl(d / "status.json")
    assert winacl.effective_allow(dacl, winacl.SID_USERS, winacl.FILE_READ_DATA)
    assert not winacl.effective_allow(dacl, winacl.SID_USERS, codeacl.WRITE_RIGHTS)
    report = _status_dir_report(d, me)
    assert report.startswith("OK") and winacl.SID_USERS in report
