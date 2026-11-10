"""In-process watchdog for the recording loop (cross-platform).

systemd has ``WatchdogSec``; the Windows Service Control Manager has no equivalent — it only
notices a service whose *process* ends. So the recorder watches itself: the recording loop
calls :meth:`LoopWatchdog.beat` from every tick, and a separate thread checks the age of the
last beat. If the loop has not run for ``timeout_s`` (deadlock, a call that never returns, a
hung ledger lock), ``on_expire`` is called once. The service's handler records what it can
within a bounded time and then ends the process with a distinct exit code, so the service
manager (SCM failure actions / systemd ``Restart=always``) starts a fresh process, whose
startup recovery reconciles whatever was in flight.

The watchdog is disarmed when a stop is requested: shutdown is supervised by the service
manager's own stop timeout (and by the Windows service host's stop deadline).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


class LoopWatchdog:
    def __init__(self, timeout_s: float, on_expire: Callable[[float], None],
                 mono: Callable[[], float] = time.monotonic, check_interval_s: float | None = None) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.timeout_s = timeout_s
        self._on_expire = on_expire
        self._mono = mono
        self._interval = check_interval_s if check_interval_s is not None else min(5.0, timeout_s / 4)
        self._last = mono()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self.fired = False

    def beat(self) -> None:
        self._last = self._mono()      # a float store is atomic under the GIL; no lock needed

    @property
    def age_s(self) -> float:
        return self._mono() - self._last

    def start(self) -> None:
        """Arm the watchdog. One-shot: after :meth:`disarm`/:meth:`stop` it stays disarmed."""
        if self._done.is_set() or self._thread is not None:
            return
        self._last = self._mono()
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._thread.start()

    def disarm(self) -> None:
        """Stop checking without waiting (safe from signal handlers and SCM control handlers)."""
        self._done.set()

    def stop(self) -> None:
        self._done.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(5.0)

    def check(self) -> bool:
        """One check; returns True if the watchdog fired. Exposed for deterministic tests."""
        if self.fired or self._done.is_set():
            return False
        age = self.age_s
        if age <= self.timeout_s:
            return False
        self.fired = True
        try:
            self._on_expire(age)
        except Exception:  # the handler must never take the watchdog thread down silently
            log.exception("watchdog handler failed")
        return True

    def _run(self) -> None:
        while not self._done.wait(self._interval):
            if self.check():
                return
