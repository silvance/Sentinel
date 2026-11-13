"""Applying updates: stopping and restarting recording, the check run by the new version, the result
the window shows, and the swap script. The real swap is exercised end to end with two real builds
(docs/08 §10); these tests need no administrator rights and touch no service."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from sentinel import __version__
from sentinel.update import apply as ap
from sentinel.update import cli as ucli
from sentinel.update.package import UpdateRefused


def _sc_world(states: dict[str, str]):  # noqa: ANN202
    calls: list[list[str]] = []

    def sc(args: list[str]) -> subprocess.CompletedProcess:
        calls.append(args)
        verb, name = args[0], args[1]
        if name not in states:
            return subprocess.CompletedProcess(args, 1060, "", "")
        if verb == "stop":
            states[name] = "STOPPED"
        if verb == "start":
            states[name] = "RUNNING"
        return subprocess.CompletedProcess(args, 0, f"SERVICE_NAME: {name}\n  STATE : 4  {states[name]}\n", "")
    return sc, calls


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows services")
def test_services_are_stopped_and_started_again_in_order() -> None:
    states = {"SentinelRecorder": "RUNNING", "SentinelArchive": "RUNNING"}
    sc, calls = _sc_world(states)
    st = ap.stop_recording(None, sc=sc, sleep=lambda _s: None)
    assert st.service_managed and st.services == ["SentinelRecorder", "SentinelArchive"] and st.recorder_running
    stops = [c[1] for c in calls if c[0] == "stop"]
    assert stops == ["SentinelArchive", "SentinelRecorder"]          # archive copier first
    assert set(states.values()) == {"STOPPED"}
    ok, _ = ap.start_recording(st, None, sc=sc, sleep=lambda _s: None)
    assert ok and [c[1] for c in calls if c[0] == "start"] == ["SentinelRecorder", "SentinelArchive"]


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows services")
def test_a_service_that_does_not_stop_cancels_the_update() -> None:
    asked = []

    def sc(args: list[str]) -> subprocess.CompletedProcess:      # running until asked, then stuck stopping
        if args[0] == "stop":
            asked.append(args[1])
        state = "3  STOP_PENDING" if args[1] in asked else "4  RUNNING"
        return subprocess.CompletedProcess(args, 0, f"STATE : {state}", "")
    with pytest.raises(UpdateRefused) as e:
        ap.stop_recording(None, sc=sc, sleep=lambda _s: None, timeout_s=0.0)
    assert e.value.code == "not_stopped"


class FakeControl:
    def __init__(self, running: bool, stops: bool = True) -> None:
        self.running, self.stops, self.started = running, stops, 0

    def is_running(self) -> bool:
        return self.running

    def stop(self, _t: float) -> bool:
        if self.stops:
            self.running = False
        return self.stops

    def start(self):  # noqa: ANN201
        self.started += 1
        from sentinel.launcher.control import StartResult

        return StartResult(True, None, "started")


def _cfg(tmp: Path) -> Path:
    from sentinel.launcher import setup
    from sentinel.paths import Layout

    root = tmp / "m"
    lay = Layout("machine", root / "config" / "sentinel.toml", root / "config" / "camera.secret", root / "spool",
                 root / "data", root / "logs", root / "status")
    path, _ = setup.write_setup(setup.SetupAnswers("192.0.2.64", "Streaming/Channels/101", camera_name="Door",
                                                   timezone="UTC"), lay)
    return path


def test_background_recorder_is_stopped_and_restarted(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    ctl = FakeControl(running=True)
    sc, _ = _sc_world({})
    st = ap.stop_recording(cfg, control_factory=lambda *_a: ctl, sc=sc)
    assert st.recorder_running and not st.service_managed and not ctl.running
    ok, _ = ap.start_recording(st, cfg, control_factory=lambda *_a: ctl, sc=sc)
    assert ok and ctl.started == 1
    with pytest.raises(UpdateRefused):
        ap.stop_recording(cfg, control_factory=lambda *_a: FakeControl(True, stops=False), sc=sc)


def test_a_recorder_that_was_not_running_is_not_started(tmp_path: Path) -> None:
    ctl = FakeControl(running=False)
    ok, detail = ap.start_recording(ap.RecordingState(), _cfg(tmp_path), control_factory=lambda *_a: ctl)
    assert ok and ctl.started == 0 and "not running" in detail


# ---------------------------------------------------------------------- the new version's check
def _installed(folder: Path, version: str = __version__, files: dict[str, bytes] | None = None) -> Path:
    files = files or {"Sentinel.exe": b"exe", "_internal/x.pyd": b"x"}
    for rel, data in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_bytes(data)
    (folder / "BUILD-MANIFEST.json").write_text(json.dumps(
        {"version": version, "files": {r: hashlib.sha256(d).hexdigest() for r, d in files.items()}}), "utf-8")
    return folder


def _pending(app: Path, cfg: Path | None, action: str = "update", **kw) -> Path:
    folders = ap.Folders(app)
    p = ap.Pending(action, "0.3.0", __version__, str(app), str(folders.staging(__version__)), str(folders.previous),
                   str(cfg) if cfg else None, asdict(ap.RecordingState()), package_sha256="ab" * 32, who="tech",
                   **kw)
    p.write(folders.state)
    return folders.state


def test_finish_keeps_a_good_version_and_records_it(tmp_path: Path) -> None:
    app = _installed(tmp_path / "Sentinel")
    cfg = _cfg(tmp_path)
    assert ucli.cmd_finish(argparse.Namespace(state=str(_pending(app, cfg)))) == 0
    res = ap.read_result(ap.Folders(app))
    assert res["outcome"] == "updated" and res["watch"] and not res["shown"] and res["to_version"] == __version__
    from sentinel.config import load_config
    from sentinel.db import Ledger

    led = Ledger(load_config(cfg).data_path / "sentinel.db")
    ev = [r for r in led.query("SELECT code, severity, detail FROM events WHERE category='software'")]
    led.close()
    assert ev and ev[-1][0] == "software_updated" and ev[-1][1] == "audit" and "ab" * 32 in ev[-1][2]


def test_finish_fails_on_a_changed_file_and_says_why(tmp_path: Path) -> None:
    app = _installed(tmp_path / "Sentinel")
    (app / "_internal/x.pyd").write_bytes(b"damaged")
    state = _pending(app, None)
    assert ucli.cmd_finish(argparse.Namespace(state=str(state))) == 1
    assert "x.pyd: changed" in ap.Pending.read(state).reason


def test_finish_fails_when_the_manifest_is_another_version(tmp_path: Path) -> None:
    app = _installed(tmp_path / "Sentinel", version="9.9.9")
    assert ucli.cmd_finish(argparse.Namespace(state=str(_pending(app, None)))) == 1


def test_restart_after_a_failed_update_says_what_happened(tmp_path: Path) -> None:
    app = _installed(tmp_path / "Sentinel")
    state = _pending(app, None, reason="the recorder did not start: C-001")
    assert ucli.cmd_restart(argparse.Namespace(state=str(state), outcome="rolled_back", reason="x")) == 0
    res = ap.read_result(ap.Folders(app))
    assert res["outcome"] == "rolled_back" and "C-001" in res["message"] and "undone" in res["message"]


def test_result_is_marked_shown(tmp_path: Path) -> None:
    folders = ap.Folders(tmp_path / "Sentinel")
    ap.write_result(folders, "updated", None, "ok", watch=True)
    ap.mark_result(folders, shown=True, watch=False)
    assert ap.read_result(folders)["shown"] and not ap.read_result(folders)["watch"]


# ---------------------------------------------------------------------- the swap script
def test_finisher_script_quotes_paths_and_goes_back_on_failure(tmp_path: Path) -> None:
    app = tmp_path / "O'Brien's PC" / "Sentinel"
    folders = ap.Folders(app)
    p = ap.Pending("update", "0.3.0", "0.3.1", str(app), str(folders.staging("0.3.1")), str(folders.previous),
                   None, asdict(ap.RecordingState()))
    s = ap.finisher_script(p, folders, 1234)
    assert "O''Brien''s PC" in s and "O'Brien's" not in s.replace("O''Brien''s", "")
    assert "Wait-Process -Id 1234" in s
    for step in ("Move-Item -LiteralPath $app -Destination $prev", "update finish --state $state",
                 "Move-Item -LiteralPath $prev -Destination $app", "--outcome rolled_back",
                 "--outcome aborted", "Get-Acl -LiteralPath $app | Set-Acl -LiteralPath $staged"):
        assert step in s, step
    assert s.index("update finish") < s.index("--outcome rolled_back")
    # the version kept from before is set aside, restored if the new one fails, deleted only after success
    assert "Move-Item -LiteralPath $prev -Destination $prevOld" in s
    assert s.count("Move-Item -LiteralPath $prevOld -Destination $prev") == 2       # swap failed / check failed
    assert s.rindex("Remove-Item -LiteralPath $prevOld") > s.index("update finish")
    assert "Remove-Item -LiteralPath $staged" in s                    # an update removes its staging when cancelled
    back = ap.Pending("rollback", "0.3.1", "0.3.0", str(app), str(folders.previous), str(folders.previous), None,
                      asdict(ap.RecordingState()))
    assert "Remove-Item -LiteralPath $staged" not in ap.finisher_script(back, folders, None)  # never the kept version
    p.reopen = False
    assert "Start-Process -FilePath $exe" not in ap.finisher_script(p, folders, None)


def test_work_folders_sit_next_to_the_program_folder(tmp_path: Path) -> None:
    f = ap.Folders(tmp_path / "Program Files" / "Sentinel")
    assert f.work.name == "Sentinel.updates" and f.previous.name == "Sentinel.previous"
    assert f.staging("0.3.1").name == "Sentinel.update-0.3.1" and f.work.parent == f.app.parent


def test_dropping_an_update_file_on_sentinel_runs_update_apply(monkeypatch) -> None:
    from sentinel.launcher import cli as lcli

    seen = []
    monkeypatch.setattr(ucli, "main", lambda argv: seen.append(argv) or 0)
    assert lcli.main([r"D:\Sentinel-update-0.3.1.sntu"]) == 0
    assert seen == [["apply", r"D:\Sentinel-update-0.3.1.sntu"]]


# ---------------------------------------------------------------------- field report 2026-11-11 (laptop)
def test_only_the_recorder_blocks_the_swap_and_a_click_cannot_pause_it(tmp_path: Path) -> None:
    """The archive copier (a background 'archive run' process) was taken for a recorder that did not
    stop, so the update cancelled itself; and a click in the window froze it (QuickEdit)."""
    app = tmp_path / "Sentinel"
    folders = ap.Folders(app)
    p = ap.Pending("update", "0.3.0", "0.3.4", str(app), str(folders.staging("0.3.4")), str(folders.previous),
                   None, asdict(ap.RecordingState()))
    s = ap.finisher_script(p, folders, None)
    block = s[s.index("function Recorders"):s.index("function Recorders") + 200]
    assert r"recorder\s+run" in block and "archive" not in block
    assert "SetConsoleMode" in s and "0x40" in s and s.index("SetConsoleMode") < s.index("Say 'Recording has been")
    assert s.index("@(Recorders).Count -gt 0) {") < s.index("$others = @(From-App)") < s.index("Move-Item")
    assert "Stop-Process -Id $_.ProcessId" in s


def test_the_window_can_tell_an_update_is_under_way(tmp_path: Path) -> None:
    folders = ap.Folders(tmp_path / "Sentinel")
    assert ap.in_progress(folders) is None
    _pending(folders.app, None)
    assert ap.in_progress(folders) == f"Updating to Sentinel {__version__}"
    assert ap.in_progress(folders, now=folders.state.stat().st_mtime + 7200) is None      # stale: ignored


def test_process_counts_survive_windows_powershell_unrolling(tmp_path: Path) -> None:
    """End-to-end test 2026-11-11: in Windows PowerShell 5.1 a function returning ONE process yields a
    bare CIM object with no .Count, so a single recorder still running counted as none."""
    import re

    app = tmp_path / "Sentinel"
    folders = ap.Folders(app)
    p = ap.Pending("update", "0.3.4", "0.3.5", str(app), str(folders.staging("0.3.5")), str(folders.previous),
                   None, asdict(ap.RecordingState()))
    s = ap.finisher_script(p, folders, None)
    assert not re.search(r"(?<!@)\((From-App|Recorders)\)\.Count", s)
    assert "$others = @(From-App)" in s and s.count("@(Recorders).Count") == 2


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows services")
def test_a_stop_that_gives_up_starts_the_services_again() -> None:
    """Review 2026-11-12: the update gave up after 90 s, said 'Nothing was changed' and left both services
    stopped (a requested stop is never restarted by the service manager)."""
    asked, started = [], []

    def sc(args: list[str]) -> subprocess.CompletedProcess:
        if args[0] == "stop":
            asked.append(args[1])
        if args[0] == "start":
            started.append(args[1])
            return subprocess.CompletedProcess(args, 0, "", "")
        state = "4  RUNNING" if args[1] in started else "3  STOP_PENDING" if args[1] in asked else "4  RUNNING"
        return subprocess.CompletedProcess(args, 0, f"STATE : {state}", "")
    with pytest.raises(UpdateRefused) as e:
        ap.stop_recording(None, sc=sc, sleep=lambda _s: None, timeout_s=0.0)
    st = e.value.state
    assert st.service_managed and st.services == ["SentinelRecorder", "SentinelArchive"]
    assert "Nothing was changed" not in str(e.value)
    import sentinel.update.apply as apply_mod
    orig = apply_mod.start_recording
    try:
        apply_mod.start_recording = lambda s, c: orig(s, c, sc=sc, sleep=lambda _s: None)
        assert ap.restart_after_refusal(e.value, None) == "Recording was started again."
    finally:
        apply_mod.start_recording = orig
    assert started == ["SentinelRecorder", "SentinelArchive"]


# ---------------------------------------------------------------------- review 2026-11-12 (medium findings)
def _sc_states(seq: dict[str, list[str]]):  # noqa: ANN202
    """sc fake: 'start' accepted; each query returns the next state of that service (the last one repeats)."""
    calls: list[list[str]] = []

    def sc(args: list[str]) -> subprocess.CompletedProcess:
        calls.append(args)
        if args[0] in ("start", "stop"):
            return subprocess.CompletedProcess(args, 0, "", "")
        states = seq[args[1]]
        state = states.pop(0) if len(states) > 1 else states[0]
        out = f"STATE : {state}\n WIN32_EXIT_CODE : 1066 (0x42a)\n SERVICE_EXIT_CODE : 2 (0x2)\n"
        return subprocess.CompletedProcess(args, 0, out, "")
    return sc, calls


def test_a_service_that_stops_while_starting_fails_the_update_check() -> None:
    """'sc start' returns once the process connected; a new version that then fails was kept."""
    st = ap.RecordingState(service_managed=True, services=["SentinelRecorder"], recorder_running=True)
    sc, _ = _sc_states({"SentinelRecorder": ["2  START_PENDING", "1  STOPPED"]})
    ok, detail = ap.start_recording(st, None, sc=sc, sleep=lambda _s: None)
    assert not ok and "SentinelRecorder stopped while starting (exit 2)" in detail
    sc, _ = _sc_states({"SentinelRecorder": ["2  START_PENDING", "4  RUNNING", "1  STOPPED"]})
    ok, detail = ap.start_recording(st, None, sc=sc, sleep=lambda _s: None)
    assert not ok and "stopped again right after starting" in detail
    sc, _ = _sc_states({"SentinelRecorder": ["2  START_PENDING", "4  RUNNING"]})
    assert ap.start_recording(st, None, sc=sc, sleep=lambda _s: None) == (True, "services started")


def test_finish_stops_the_services_it_started_when_the_check_fails(tmp_path: Path, monkeypatch) -> None:
    """Otherwise the recorder runs from the new folder and the swap back fails ('Going back failed')."""
    app = _installed(tmp_path / "Sentinel")
    folders = ap.Folders(app)
    p = ap.Pending("update", "0.3.0", __version__, str(app), str(folders.staging(__version__)), str(folders.previous),
                   None, asdict(ap.RecordingState(True, ["SentinelRecorder", "SentinelArchive"], True)))
    p.write(folders.state)
    stopped: list[list[str]] = []
    monkeypatch.setattr(ucli, "start_recording", lambda st, c: (False, "SentinelArchive: 1053"))
    monkeypatch.setattr(ucli, "stop_services", lambda names: stopped.append(list(names)) or True)
    assert ucli.cmd_finish(argparse.Namespace(state=str(folders.state))) == 1
    assert stopped == [["SentinelRecorder", "SentinelArchive"]]
    assert "1053" in ap.Pending.read(folders.state).reason


def test_stop_services_stops_the_archive_first_and_waits() -> None:
    sc, calls = _sc_states({"SentinelRecorder": ["4  RUNNING", "1  STOPPED"], "SentinelArchive": ["1  STOPPED"]})
    assert ap.stop_services(["SentinelRecorder", "SentinelArchive"], sc=sc, sleep=lambda _s: None)
    assert [c[1] for c in calls if c[0] == "stop"] == ["SentinelArchive", "SentinelRecorder"]


def test_a_result_with_a_byte_order_mark_is_read(tmp_path: Path) -> None:
    """Windows PowerShell 5.1 'Set-Content -Encoding UTF8' writes a BOM; json.loads refused it, so 'Going back
    failed ... the previous version is in ...' was never shown."""
    folders = ap.Folders(tmp_path / "Sentinel")
    folders.work.mkdir(parents=True)
    folders.result.write_bytes(b"\xef\xbb\xbf" + json.dumps({"outcome": "failed", "message": "Going back failed"}
                                                             ).encode("utf-8"))
    assert ap.read_result(folders)["message"] == "Going back failed"
    ap.mark_result(folders, shown=True)
    assert ap.read_result(folders)["shown"] is True
    p = ap.Pending("update", "0.3.0", "0.3.1", str(folders.app), str(folders.staging("0.3.1")), str(folders.previous),
                   None, asdict(ap.RecordingState()))
    s = ap.finisher_script(p, folders, None)
    assert "Set-Content -LiteralPath $result" not in s and "New-Object Text.UTF8Encoding $false" in s


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows PowerShell")
def test_windows_powershell_writes_a_result_python_can_read(tmp_path: Path) -> None:
    import os

    ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0"
    ps = ps / "powershell.exe"
    if not ps.is_file():
        pytest.skip("no Windows PowerShell")
    folders = ap.Folders(tmp_path / "Sentinel")
    folders.work.mkdir(parents=True)
    p = ap.Pending("update", "0.3.0", "0.3.1", str(folders.app), str(folders.staging("0.3.1")), str(folders.previous),
                   None, asdict(ap.RecordingState()))
    s = ap.finisher_script(p, folders, None)
    head = s[:s.index("# Every use is wrapped")]                 # variables and functions only
    script = tmp_path / "t.ps1"
    script.write_text(head + "\nFail-Plain 'Going back failed: locked. Call technical support'\n", encoding="utf-8-sig")
    r = subprocess.run([str(ps), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not folders.result.read_bytes().startswith(b"\xef\xbb\xbf")
    res = ap.read_result(folders)
    assert res["outcome"] == "failed" and "Going back failed" in res["message"] and res["to_version"] == "0.3.1"


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows PowerShell")
def test_the_finisher_script_parses_in_windows_powershell(tmp_path: Path) -> None:
    import os

    ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0"
    ps = ps / "powershell.exe"
    if not ps.is_file():
        pytest.skip("no Windows PowerShell")
    folders = ap.Folders(tmp_path / "O'Brien" / "Sentinel")
    for action, services in (("update", ["SentinelRecorder", "SentinelArchive"]), ("rollback", [])):
        p = ap.Pending(action, "0.3.0", "0.3.1", str(folders.app), str(folders.staging("0.3.1")),
                       str(folders.previous), None, asdict(ap.RecordingState(bool(services), services, True)))
        f = tmp_path / f"{action}.ps1"
        f.write_text(ap.finisher_script(p, folders, 1234), encoding="utf-8-sig")
        check = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                 f"'{f}', [ref]$null, [ref]$e); $e | ForEach-Object {{ $_.ToString() }}; exit @($e).Count")
        r = subprocess.run([str(ps), "-NoProfile", "-Command", check], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr


def test_every_way_out_of_the_finisher_removes_its_state(tmp_path: Path) -> None:
    """A cancelled or failed update left pending.json, so the window said 'Updating…: recording restarts by
    itself' for an hour instead of the real status."""
    import re

    app = tmp_path / "Sentinel"
    folders = ap.Folders(app)
    for action in ("update", "rollback"):
        p = ap.Pending(action, "0.3.0", "0.3.1", str(app), str(folders.staging("0.3.1")), str(folders.previous),
                       None, asdict(ap.RecordingState(True, ["SentinelRecorder", "SentinelArchive"], True)))
        lines = ap.finisher_script(p, folders, None).splitlines()
        exits = [i for i, line in enumerate(lines) if re.search(r"\bexit (\d|\$rc)", line)]
        assert len(exits) == 4
        for i in exits:
            assert any("End-Update" in line for line in lines[max(0, i - 5):i]), lines[i]
        body = "\n".join(lines)
        assert "Remove-Item -LiteralPath $state" in body and "Remove-Item -LiteralPath $pidf" in body


def test_the_finisher_stops_the_services_before_going_back(tmp_path: Path) -> None:
    app = tmp_path / "Sentinel"
    folders = ap.Folders(app)
    p = ap.Pending("update", "0.3.0", "0.3.1", str(app), str(folders.staging("0.3.1")), str(folders.previous),
                   None, asdict(ap.RecordingState(True, ["SentinelRecorder", "SentinelArchive"], True)))
    s = ap.finisher_script(p, folders, None)
    assert "$services = @('SentinelRecorder', 'SentinelArchive')" in s
    back = s.index("The check failed")
    assert back < s.index("Stop-Services", back) < s.index("Move-Item -LiteralPath $app -Destination $failed")
    assert "$try -le 4" in s
    none = ap.Pending("update", "0.3.0", "0.3.1", str(app), str(folders.staging("0.3.1")), str(folders.previous),
                      None, asdict(ap.RecordingState()))
    assert "$services = @()" in ap.finisher_script(none, folders, None)


def test_a_finished_or_abandoned_update_does_not_hide_the_status(tmp_path: Path) -> None:
    folders = ap.Folders(tmp_path / "Sentinel")
    _pending(folders.app, None)
    (folders.work / ap.FINISHER_PID_NAME).write_text("4242", "utf-8")
    assert ap.in_progress(folders, alive=lambda _pid: True) == f"Updating to Sentinel {__version__}"
    assert ap.in_progress(folders, alive=lambda _pid: None) is not None          # cannot tell: as before
    assert ap.in_progress(folders, alive=lambda _pid: False) is None              # the update window is gone
    assert ap.abandoned(folders, alive=lambda _pid: False) is not None
    assert ap.abandoned(folders, alive=lambda _pid: True) is None
    ap.write_result(folders, "aborted", None, "cancelled")                        # a result after the state
    assert ap.in_progress(folders, alive=lambda _pid: True) is None
    ap.end_state(folders)
    assert not folders.state.exists() and not (folders.work / ap.FINISHER_PID_NAME).exists()


def test_launch_finisher_notes_the_window_process(tmp_path: Path) -> None:
    folders = ap.Folders(tmp_path / "Sentinel")
    ap.launch_finisher("# script", folders, popen=lambda *_a, **_k: type("P", (), {"pid": 31337})())
    assert (folders.work / ap.FINISHER_PID_NAME).read_text("utf-8") == "31337"


def test_an_old_state_file_with_more_fields_is_still_read(tmp_path: Path) -> None:
    state = _pending(tmp_path / "Sentinel", None)
    doc = json.loads(state.read_text("utf-8"))
    doc["added_later"] = 1
    state.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc).encode("utf-8"))
    assert ap.Pending.read(state).to_version == __version__


def _apply_world(tmp_path: Path, monkeypatch, fail_at: str):  # noqa: ANN202
    app = _installed(tmp_path / "Sentinel")
    st = ap.RecordingState(True, ["SentinelRecorder", "SentinelArchive"], True)
    started: list[ap.RecordingState] = []
    pkg = type("Pkg", (), {"version": "9.9.9", "path": tmp_path / "u.sntu", "sha256": "ab" * 32, "key_id": "k",
                           "summary": lambda self: "Sentinel 9.9.9"})()
    monkeypatch.setattr(ucli.sys, "platform", "win32")
    monkeypatch.setattr(ucli, "_app", lambda: app)
    monkeypatch.setattr(ucli, "_is_admin", lambda: True)
    monkeypatch.setattr(ucli, "_config", lambda _c: None)
    monkeypatch.setattr(ucli, "read_package", lambda *_a, **_k: pkg)
    monkeypatch.setattr(ucli, "stage", lambda *_a, **_k: None)
    monkeypatch.setattr(ucli, "stop_recording", lambda *_a, **_k: st)
    monkeypatch.setattr(ucli, "start_recording", lambda s, c: started.append(s) or (True, "services started"))

    def boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise OSError(1260, "This program is blocked by group policy")
    if fail_at == "launch":
        monkeypatch.setattr(ucli, "launch_finisher", boom)
    else:
        monkeypatch.setattr(ap.Pending, "write", boom)
    return ap.Folders(app), started


@pytest.mark.parametrize("fail_at", ["launch", "state"])
def test_a_failure_after_stopping_starts_recording_again(tmp_path: Path, monkeypatch, fail_at: str,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    """Only UpdateRefused restarted the services; PowerShell blocked by policy left recording stopped (and
    pending.json written) with a console that closed at once."""
    folders, started = _apply_world(tmp_path, monkeypatch, fail_at)
    rc = ucli.cmd_apply(argparse.Namespace(file=str(tmp_path / "u.sntu"), config=None, yes=True, window_pid=None))
    assert rc == ucli.EXIT_FAILED and len(started) == 1 and started[0].services == ["SentinelRecorder",
                                                                                     "SentinelArchive"]
    assert not folders.state.exists()
    res = ap.read_result(folders)
    assert res["outcome"] == "failed" and "group policy" in res["message"] and "started again" in res["message"]
    assert "Recording was started again." in capsys.readouterr().out


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows services")
def test_sc_timing_out_while_stopping_is_a_refusal_that_restarts() -> None:
    asked: list[str] = []

    def sc(args: list[str]) -> subprocess.CompletedProcess:
        if args[0] == "stop":
            asked.append(args[1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if asked:
            raise subprocess.TimeoutExpired(["sc.exe", *args], 60)
        return subprocess.CompletedProcess(args, 0, "STATE : 4  RUNNING", "")
    with pytest.raises(UpdateRefused) as e:
        ap.stop_recording(None, sc=sc, sleep=lambda _s: None)
    assert e.value.code == "not_stopped" and e.value.state.services == ["SentinelRecorder", "SentinelArchive"]


def test_a_new_update_restarts_what_an_abandoned_one_stopped(tmp_path: Path) -> None:
    folders = ap.Folders(tmp_path / "Sentinel")
    p = ap.Pending("update", "0.3.0", "0.3.1", str(folders.app), "s", "p", None,
                   asdict(ap.RecordingState(True, ["SentinelRecorder", "SentinelArchive"], True)))
    p.write(folders.state)
    (folders.work / ap.FINISHER_PID_NAME).write_text("1", "utf-8")
    import sentinel.update.apply as apply_mod

    orig = apply_mod._process_alive
    try:
        apply_mod._process_alive = lambda _pid: False
        assert ucli._busy(folders) is None
        st = ucli._take_over(ap.RecordingState(True, [], False), folders)
    finally:
        apply_mod._process_alive = orig
    assert st.services == ["SentinelRecorder", "SentinelArchive"] and st.recorder_running


def test_the_update_waits_as_long_as_the_services_may_take_to_stop() -> None:
    from sentinel.winsvc.scm import ServiceHost
    import inspect

    deadline = inspect.signature(ServiceHost.__init__).parameters["stop_deadline_s"].default
    assert ap.STOP_TIMEOUT_S > deadline
