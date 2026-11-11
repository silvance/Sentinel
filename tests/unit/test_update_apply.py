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
    ok, _ = ap.start_recording(st, None, sc=sc)
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
    assert s.index("(Recorders).Count -gt 0) {") < s.index("$others = From-App") < s.index("Move-Item")
    assert "Stop-Process -Id $_.ProcessId" in s


def test_the_window_can_tell_an_update_is_under_way(tmp_path: Path) -> None:
    folders = ap.Folders(tmp_path / "Sentinel")
    assert ap.in_progress(folders) is None
    _pending(folders.app, None)
    assert ap.in_progress(folders) == f"Updating to Sentinel {__version__}"
    assert ap.in_progress(folders, now=folders.state.stat().st_mtime + 7200) is None      # stale: ignored
