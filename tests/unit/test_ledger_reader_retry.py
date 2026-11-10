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


def test_non_transient_errors_are_not_retried(tmp_path: Path) -> None:
    ro = Ledger(_make(tmp_path), readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            ro.query("SELECT * FROM nonexistent")
    finally:
        ro.close()
