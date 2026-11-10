"""What the operator sees after starting Sentinel: the measured recorder status.

Normally the operator screen (``sentinel.gui``: status, dark mode, "Lock this computer"). The
console banner — the same high-contrast text the ``status`` command prints, refreshed every few
seconds — is the fallback: for ``launch --once`` (health checks), when the recorder could not be
started, when the GUI library is not available, when there is no display, and when
``SENTINEL_TEXT_STATUS=1`` is set. Both read only the status files (heartbeat-checked), so what
they show is what the recorder measured, never "the process exists, therefore green".

Double-clicked ``Sentinel.exe`` owns its console window; it is hidden once the operator screen
opens (a console the program was started from, e.g. a technician's Command Prompt, is left alone).
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TextIO

from ..alerts import Severity, make_alert
from ..service import banner
from ..status import read_status

HEALTHY = ("RECORDING", "STORAGE_WARNING", "PAUSED")   # PAUSED: planned (recording schedule)
CLOSE_NOTE = "Recording continues in the background when this window is closed."


class StatusView(Protocol):
    def run(self, status_path: Path, camera_name: str, started_after: float | None = None,
            once: bool = False, start_problem: str | None = None) -> int: ...


def message_banner(title: str, body: str) -> str:
    return banner({"overall": title, "alert": {"message": body, "severity": int(Severity.NOTICE)}}, None)


def alert_banner(code: str) -> str:
    alert = make_alert(code, Severity.WARNING if code == "C-002" else Severity.CRITICAL)
    return banner({"alert": alert.to_dict()}, None)


class ConsoleStatusView:
    def __init__(self, out: TextIO | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, refresh_s: float = 2.0,
                 startup_grace_s: float = 30.0) -> None:
        self.out = out or sys.stdout
        self.clock, self.sleep = clock, sleep
        self.refresh_s, self.startup_grace_s = refresh_s, startup_grace_s
        self._ansi = False

    def render(self, status_path: Path, camera_name: str, started_after: float | None = None,
               start_problem: str | None = None) -> tuple[str, bool]:
        """(screen text, healthy)."""
        if start_problem:
            return alert_banner(start_problem), False
        doc, problem = read_status(status_path)
        fresh = doc is not None and float(doc.get("heartbeat_epoch", 0)) >= (started_after or 0) - 1.0
        if started_after is not None and not fresh and self.clock() - started_after < self.startup_grace_s:
            text = message_banner("STARTING", "The recorder is starting. Please wait.")
            healthy = False
        else:
            text = banner(doc, problem)
            healthy = doc is not None and not problem and doc.get("overall") in HEALTHY
        stamp = datetime.fromtimestamp(self.clock()).strftime("%H:%M:%S")
        lines = [text, "", f"Camera: {camera_name or 'Camera'}    Checked at {stamp}"]
        if doc is not None and not problem:
            lines.append(CLOSE_NOTE)
        return "\n".join(lines), healthy

    def _clear(self) -> None:
        if self._ansi:
            self.out.write("\x1b[2J\x1b[H")
        else:
            self.out.write("\n" * 2)

    def run(self, status_path: Path, camera_name: str, started_after: float | None = None,
            once: bool = False, start_problem: str | None = None) -> int:
        if not once:
            self._ansi = enable_console_ansi()
        try:
            while True:
                text, healthy = self.render(status_path, camera_name, started_after, start_problem)
                if not once:
                    self._clear()
                self.out.write(text + "\n")
                self.out.flush()
                if once:
                    return 0 if healthy else 4
                self.sleep(self.refresh_s)
        except KeyboardInterrupt:
            return 0


def enable_console_ansi() -> bool:
    """Turn on VT escape processing for this process's own console (Windows 10+). False if unavailable."""
    if sys.platform != "win32":
        return sys.stdout.isatty()
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.restype = wintypes.HANDLE
        handle = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except (OSError, AttributeError):
        return False


def set_console_title(title: str) -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.WinDLL("kernel32").SetConsoleTitleW(title)
        except (OSError, AttributeError):
            pass


def hide_own_console() -> bool:
    """Hide the console window only if this process is the only one attached to it (it was
    created for this double-clicked program). True if hidden."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32")
        hwnd = k32.GetConsoleWindow()
        if not hwnd:
            return False
        procs = (ctypes.c_uint32 * 4)()
        if k32.GetConsoleProcessList(procs, 4) != 1:
            return False
        ctypes.WinDLL("user32").ShowWindow(hwnd, 0)   # SW_HIDE
        return True
    except (OSError, AttributeError):
        return False


class GuiStatusView:
    """The operator screen as the launcher's view. It never controls the recorder."""

    def __init__(self, cfg: Any, console: ConsoleStatusView | None = None,
                 wait_s: float = 30.0, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.console = console or ConsoleStatusView()
        self.wait_s, self.clock, self.sleep = wait_s, clock, sleep

    def _wait_for_first_status(self, status_path: Path, started_after: float) -> None:
        """Just started the recorder: show "starting" in the console until its first heartbeat,
        so the screen does not open on "recorder not responding"."""
        print(message_banner("STARTING", "The recorder is starting. Please wait."), flush=True)
        while self.clock() - started_after < self.wait_s:
            doc, _ = read_status(status_path, max_age_s=float("inf"))
            if doc is not None and float(doc.get("heartbeat_epoch", 0)) >= started_after - 1.0:
                return
            self.sleep(0.5)

    def run(self, status_path: Path, camera_name: str, started_after: float | None = None,
            once: bool = False, start_problem: str | None = None) -> int:
        if once or start_problem:
            return self.console.run(status_path, camera_name, started_after, once, start_problem)
        if started_after is not None:
            self._wait_for_first_status(status_path, started_after)
        from ..gui.__main__ import run_window

        hide_own_console()
        return run_window(self.cfg)


def gui_view_provider(cfg: Any) -> StatusView | None:
    if cfg is None or os.environ.get("SENTINEL_TEXT_STATUS") == "1":
        return None
    if sys.platform != "win32" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    try:
        import PySide6.QtWidgets  # noqa: F401
    except ImportError:
        return None
    return GuiStatusView(cfg)


# Providers are tried in order; signature (cfg) -> StatusView | None. Console view is the fallback.
VIEW_PROVIDERS: list[Callable[[Any], Any]] = [gui_view_provider]


def select_view(cfg: Any = None) -> StatusView:
    for provider in VIEW_PROVIDERS:
        view = provider(cfg)
        if view is not None:
            return view
    return ConsoleStatusView()
