"""Sentinel.exe opens the operator screen (sentinel.launcher.view.GuiStatusView); the console
banner remains for --once, start problems, no GUI library and SENTINEL_TEXT_STATUS=1."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from conftest import make_config
from sentinel.launcher import cli, view


class FakeConsole(view.ConsoleStatusView):
    def __init__(self) -> None:
        super().__init__(out=io.StringIO())
        self.calls: list[tuple] = []

    def run(self, *a: Any, **k: Any) -> int:      # type: ignore[override]
        self.calls.append((a, k))
        return 7


def test_provider_respects_the_text_switch_and_missing_config(monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setenv("SENTINEL_TEXT_STATUS", "1")
    assert view.gui_view_provider(cfg) is None
    assert isinstance(view.select_view(cfg), view.ConsoleStatusView)
    monkeypatch.setenv("SENTINEL_TEXT_STATUS", "0")
    assert view.gui_view_provider(None) is None
    pytest.importorskip("PySide6")
    monkeypatch.setattr(view.sys, "platform", "win32")
    assert isinstance(view.gui_view_provider(cfg), view.GuiStatusView)


def test_provider_falls_back_without_the_gui_library(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import builtins

    real = builtins.__import__

    def no_qt(name: str, *a: Any, **k: Any) -> Any:
        if name.startswith("PySide6"):
            raise ImportError("no Qt")
        return real(name, *a, **k)

    monkeypatch.setenv("SENTINEL_TEXT_STATUS", "0")
    monkeypatch.setattr(builtins, "__import__", no_qt)
    assert view.gui_view_provider(make_config(tmp_path)) is None


@pytest.mark.parametrize("kw", [{"once": True}, {"start_problem": "C-003"}])
def test_health_checks_and_start_problems_stay_in_the_console(tmp_path: Path, kw: dict) -> None:
    console = FakeConsole()
    v = view.GuiStatusView(make_config(tmp_path), console=console)
    assert v.run(tmp_path / "status.json", "Cam", **kw) == 7 and len(console.calls) == 1


def test_opens_the_screen_after_the_first_heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sentinel.gui.__main__ as gui_main

    cfg = make_config(tmp_path)
    status = cfg.status_path
    status.parent.mkdir(parents=True, exist_ok=True)
    t = {"now": 1000.0}
    opened: list[Any] = []
    monkeypatch.setattr(gui_main, "run_window", lambda c, **_k: opened.append((c, t["now"])) or 0)
    monkeypatch.setattr(view, "hide_own_console", lambda: True)

    def sleep(s: float) -> None:
        t["now"] += s
        if t["now"] >= 1003.0:                       # the recorder writes its first heartbeat
            status.write_text(json.dumps({"heartbeat_epoch": 1002.5}), encoding="utf-8")

    v = view.GuiStatusView(cfg, wait_s=30.0, clock=lambda: t["now"], sleep=sleep)
    assert v.run(status, "Cam", started_after=1000.0) == 0
    assert opened and opened[0][0] is cfg and 1003.0 <= opened[0][1] < 1004.0


def test_hide_own_console_leaves_a_shared_console_alone() -> None:
    # The test runner shares its console with its parent shell (or has none): never hidden.
    assert view.hide_own_console() is False


def test_admin_pin_command_forwards_to_the_screen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sentinel.gui.__main__ as gui_main

    cfgfile = tmp_path / "sentinel.toml"
    cfgfile.write_text("", encoding="utf-8")
    seen: list[list[str]] = []
    monkeypatch.setattr(gui_main, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["admin-pin", "--config", str(cfgfile)]) == 0
    assert seen == [["--config", str(cfgfile.resolve()), "--set-admin-pin"]] or seen == [
        ["--config", str(cfgfile), "--set-admin-pin"]]
