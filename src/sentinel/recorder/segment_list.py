"""Incremental reader for FFmpeg's CSV segment list.

FFmpeg appends ``filename,start,end`` when (and only when) a segment is closed. A line
without a trailing newline may be mid-write and is not consumed until complete.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SegmentEntry:
    filename: str
    start_s: float
    end_s: float


class SegmentListReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self.bad_lines: list[str] = []

    def read_new(self) -> list[SegmentEntry]:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self._offset)
                data = fh.read()
        except FileNotFoundError:
            return []
        if not data:
            return []
        last_nl = data.rfind(b"\n")
        if last_nl < 0:
            return []
        complete = data[: last_nl + 1]
        self._offset += len(complete)
        return self._parse(complete.decode("utf-8", "replace"))

    def read_all(self) -> list[SegmentEntry]:
        self._offset = 0
        return self.read_new()

    def _parse(self, text: str) -> list[SegmentEntry]:
        out = []
        for row in csv.reader(io.StringIO(text)):
            if not row:
                continue
            try:
                out.append(SegmentEntry(row[0], float(row[1]), float(row[2])))
            except (IndexError, ValueError):
                self.bad_lines.append(",".join(row))
        return out
