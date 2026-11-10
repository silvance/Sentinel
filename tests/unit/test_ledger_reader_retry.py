"""Readers must survive the transient window right after the recorder process dies.

Measured on Windows (docs/branches/fix-ledger-reader-retry.md): for ~1 s after a hard kill,
opening or querying the WAL ledger can raise "disk I/O error"; a second later it works.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import sentinel.db as db_mod
from sentinel.db import Ledger


def _make(tmp_path: Path) -> Path:
    led = Ledger(tmp_path / "s.db")
    led.add_event("info", "test", "x", "hello")
    led.close()
    return tmp_path / "s.db"


class _Flaky:
    """Wraps a real connection; the first ``n`` executes raise a transient error."""

    def __init__(self, real: sqlite3.Connection, failures: dict) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_failures", failures)

    def __setattr__(self, name: str, value: object) -> None:  # e.g. row_factory goes to the real connection
        setattr(self._real, name, value)

    def execute(self, *a, **kw):  # noqa: ANN002, ANN003
        if self._failures["left"] > 0 and not a[0].startswith("PRAGMA"):
            self._failures["left"] -= 1
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(*a, **kw)

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(self._real, name)


def test_readonly_query_retries_transient_io_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _make(tmp_path)
    failures = {"left": 3}
    real_connect = sqlite3.connect
    monkeypatch.setattr(db_mod.sqlite3, "connect", lambda *a, **kw: _Flaky(real_connect(*a, **kw), failures))
    monkeypatch.setattr(db_mod.time, "sleep", lambda s: None)
    ro = Ledger(path, readonly=True)
    try:
        assert ro.query("SELECT code FROM events")[0]["code"] == "x"
    finally:
        ro.close()
    assert failures["left"] == 0


def test_readonly_gives_up_after_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _make(tmp_path)
    failures = {"left": 10_000}
    real_connect = sqlite3.connect
    monkeypatch.setattr(db_mod.sqlite3, "connect", lambda *a, **kw: _Flaky(real_connect(*a, **kw), failures))
    monkeypatch.setattr(db_mod, "READ_RETRY_SECONDS", 0.3)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        Ledger(path, readonly=True).query("SELECT 1")


def test_query_reopens_after_transient_failure_mid_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failure AFTER a successful open: query() must reopen the connection and retry."""
    path = _make(tmp_path)
    failures = {"left": 0}
    real_connect = sqlite3.connect
    opened = {"n": 0}

    def connect(*a, **kw):  # noqa: ANN002, ANN003
        opened["n"] += 1
        return _Flaky(real_connect(*a, **kw), failures)

    monkeypatch.setattr(db_mod.sqlite3, "connect", connect)
    monkeypatch.setattr(db_mod.time, "sleep", lambda s: None)
    ro = Ledger(path, readonly=True)
    try:
        assert opened["n"] == 1
        failures["left"] = 2                       # the recorder dies now
        assert ro.query("SELECT code FROM events")[0]["code"] == "x"
        assert opened["n"] >= 2, "a fresh connection should have been opened"
    finally:
        ro.close()


def test_retry_does_not_hold_lock_while_sleeping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _make(tmp_path)
    failures = {"left": 0}
    real_connect = sqlite3.connect
    monkeypatch.setattr(db_mod.sqlite3, "connect", lambda *a, **kw: _Flaky(real_connect(*a, **kw), failures))
    ro = Ledger(path, readonly=True)
    held_while_sleeping = []

    def sleep(_s: float) -> None:
        acquired = ro._lock.acquire(blocking=False)   # another thread could use the ledger now
        held_while_sleeping.append(not acquired)
        if acquired:
            ro._lock.release()

    monkeypatch.setattr(db_mod.time, "sleep", sleep)
    try:
        failures["left"] = 2
        ro.query("SELECT 1")
    finally:
        ro.close()
    assert held_while_sleeping and not any(held_while_sleeping)


def test_zero_retry_budget_fails_fast_and_closed_ledger_raises(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    path = _make(tmp_path)
    failures = {"left": 0}
    real_connect = sqlite3.connect
    monkeypatch.setattr(db_mod.sqlite3, "connect", lambda *a, **kw: _Flaky(real_connect(*a, **kw), failures))
    ro = Ledger(path, readonly=True, retry_seconds=0)
    failures["left"] = 1
    with pytest.raises(sqlite3.OperationalError):
        ro.query("SELECT 1")
    ro.close()
    with pytest.raises(sqlite3.ProgrammingError):
        ro.query("SELECT 1")


def test_non_transient_errors_are_not_retried(tmp_path: Path) -> None:
    ro = Ledger(_make(tmp_path), readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            ro.query("SELECT * FROM nonexistent")
    finally:
        ro.close()
