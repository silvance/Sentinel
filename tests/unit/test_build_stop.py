"""The build stops Sentinel programs running from the folder it replaces (owner request
2026-11-10: a test run left the archive copier running and the next build failed with
"Access is denied"). Only that folder is touched."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _build():  # noqa: ANN202
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        spec = importlib.util.spec_from_file_location("sentinel_packaging_build_stop", ROOT / "packaging" / "build.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(ROOT / "packaging"))


build = _build()


def test_only_programs_inside_the_output_folder(tmp_path: Path) -> None:
    app = tmp_path / "dist" / "Sentinel"
    app.mkdir(parents=True)
    inside = str(app / "Sentinel.exe")
    procs = [(1, inside), (2, str(app / "_internal" / "x.exe")),
             (3, str(tmp_path / "dist" / "Sentinel2" / "Sentinel.exe")),        # sibling with the same prefix
             (4, r"C:\Program Files\Sentinel\Sentinel.exe")]
    if os.name == "nt":
        procs.append((5, inside.upper()))                                      # Windows paths ignore case
    got = [pid for pid, _ in build.running_from(app, procs)]
    assert got == ([1, 2, 5] if os.name == "nt" else [1, 2])


def test_graceful_stop_first_then_the_rest_are_ended(tmp_path: Path) -> None:
    app = tmp_path / "Sentinel"
    state = {"running": {10: "Sentinel.exe recorder", 11: "Sentinel.exe archive run"}}
    calls: list[str] = []

    def graceful() -> None:
        calls.append("stop")
        state["running"].pop(10)                                               # the recorder stops itself

    def kill(pid: int) -> None:
        calls.append(f"kill {pid}")
        state["running"].pop(pid)

    stopped = build.stop_running_copies(app, processes=lambda: list(state["running"].items()), kill=kill,
                                        graceful=graceful, sleep=lambda _s: None)
    assert stopped == [10, 11] and calls == ["stop", "kill 11"]


def test_nothing_running_does_nothing(tmp_path: Path) -> None:
    called: list[str] = []
    assert build.stop_running_copies(tmp_path, processes=lambda: [], kill=lambda p: called.append("k"),
                                     graceful=lambda: called.append("g")) == []
    assert called == []


def test_a_program_that_will_not_stop_fails_the_build(tmp_path: Path) -> None:
    with pytest.raises(build.BuildError, match="could not be stopped"):
        build.stop_running_copies(tmp_path, wait_s=0.0, processes=lambda: [(7, "x.exe")], kill=lambda _p: None,
                                  graceful=lambda: None, sleep=lambda _s: None)
