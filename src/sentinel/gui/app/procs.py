"""Running Sentinel's own commands from the window, and background work off the UI thread."""

from __future__ import annotations

import codecs
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from ...paths import is_frozen


def sentinel_argv(*args: str) -> list[str]:
    """Command line for ``Sentinel.exe <args>`` (frozen) or ``python -m sentinel.launcher <args>``."""
    if is_frozen():
        return [sys.executable, *args]
    return [sys.executable, "-m", "sentinel.launcher", *args]


def service_argv(*args: str) -> list[str]:
    """Command line for ``SentinelService.exe <args>`` (frozen) or ``python -m sentinel.winsvc``."""
    if is_frozen():
        exe = Path(sys.executable).with_name("SentinelService.exe" if sys.platform == "win32" else "sentinel-service")
        return [str(exe), *args]
    return [sys.executable, "-m", "sentinel.winsvc", *args]


class Command(QObject):
    """One child process; output arrives line by line (``output``), then ``finished(exit code)``."""

    output = Signal(str)
    finished = Signal(int)

    def __init__(self, argv: list[str], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.argv = argv
        self.text: list[str] = []
        self.proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("SENTINEL_TEXT_STATUS", "1")
        if not is_frozen():   # source checkout: the child must import this same package
            src = str(Path(__file__).resolve().parents[3])
            env.insert("PYTHONPATH", os.pathsep.join(x for x in (src, env.value("PYTHONPATH")) if x))
        self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._done)
        self.proc.errorOccurred.connect(self._error)
        self._buf = ""
        self._ended = False
        # Characters split across two reads must not become "?" (review 2026-11-10).
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def start(self) -> Command:
        self.proc.start(self.argv[0], self.argv[1:])
        return self

    def running(self) -> bool:
        return self.proc.state() != QProcess.ProcessState.NotRunning

    def kill(self) -> None:
        if self.running():
            self.proc.kill()

    def _read(self) -> None:
        self._buf += self._decoder.decode(bytes(self.proc.readAllStandardOutput().data()))
        *lines, self._buf = self._buf.replace("\r\n", "\n").split("\n")
        for ln in lines:
            self.text.append(ln)
            self.output.emit(ln)

    def _done(self, code: int, _status: Any = None) -> None:
        if self._ended:
            return
        self._read()
        self._buf += self._decoder.decode(b"", final=True)
        if self._buf:
            self.text.append(self._buf)
            self.output.emit(self._buf)
            self._buf = ""
        self._ended = True
        self.finished.emit(int(code))

    def _error(self, err: QProcess.ProcessError) -> None:
        if err == QProcess.ProcessError.FailedToStart and not self._ended:
            self.output.emit(f"Could not start {os.path.basename(self.argv[0])}: {self.proc.errorString()}")
            self._ended = True
            self.finished.emit(-1)


class Task(QObject):
    """Run ``fn`` on a worker thread; ``done(result, error)`` arrives on the UI thread."""

    done = Signal(object, object)

    def __init__(self, fn: Callable[[], Any], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.fn = fn

    def start(self) -> Task:
        self.thread = threading.Thread(target=self._run, name="sentinel-gui-task", daemon=True)
        self.thread.start()
        return self

    def running(self) -> bool:
        t = getattr(self, "thread", None)
        return t is not None and t.is_alive()

    def _run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # noqa: BLE001 - reported to the person, never swallowed
            self.done.emit(None, exc)
            return
        self.done.emit(result, None)
