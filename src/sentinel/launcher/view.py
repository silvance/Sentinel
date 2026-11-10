"""What the operator sees after starting Sentinel: the measured recorder status.

There is no GUI on main yet, so the view is the same high-contrast banner the ``status``
command prints, refreshed in the console window every few seconds. It reads only
``status.json`` (heartbeat-checked by :func:`sentinel.status.read_status`), so what it shows
is what the recorder measured, never "the process exists, therefore green".

To plug in the operator GUI (branch req/operator-gui), add a provider to ``VIEW_PROVIDERS``
that returns an object with ``run(...) -> int``; the console view stays as the fallback.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TextIO

from ..alerts import Severity, make_alert
from ..service import banner
from ..status import read_status

HEALTHY = ("RECORDING", "STORAGE_WARNING")
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


# Providers are tried in order; signature () -> StatusView | None. Console view is the fallback.
VIEW_PROVIDERS: list[Callable[[], Any]] = []


def select_view() -> StatusView:
    for provider in VIEW_PROVIDERS:
        view = provider()
        if view is not None:
            return view
    return ConsoleStatusView()
