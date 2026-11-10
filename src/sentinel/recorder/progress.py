"""Parser for FFmpeg ``-progress`` key=value blocks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressSnapshot:
    out_time_s: float | None     # output stream time; None until media is muxed
    frame: int | None
    fps: float | None
    speed: str | None
    ended: bool                  # progress=end


class ProgressParser:
    """Feed lines; returns a snapshot each time a block completes."""

    def __init__(self) -> None:
        self._cur: dict[str, str] = {}

    def feed(self, line: str) -> ProgressSnapshot | None:
        line = line.strip()
        if not line or "=" not in line:
            return None
        key, _, value = line.partition("=")
        self._cur[key.strip()] = value.strip()
        if key != "progress":
            return None
        block, self._cur = self._cur, {}
        return ProgressSnapshot(
            out_time_s=_out_time(block),
            frame=_int(block.get("frame")),
            fps=_float(block.get("fps")),
            speed=block.get("speed"),
            ended=value.strip() == "end",
        )


def _out_time(block: dict[str, str]) -> float | None:
    us = _int(block.get("out_time_us"))
    if us is None:
        us = _int(block.get("out_time_ms"))  # FFmpeg historically reports µs under this key too
    if us is None or us < 0:
        return None
    return us / 1_000_000


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v not in (None, "N/A", "") else None
    except ValueError:
        return None


def _float(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "N/A", "") else None
    except ValueError:
        return None
