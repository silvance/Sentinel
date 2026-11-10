"""Shared helpers for archive-drive tests: fake drives (temp folders), fault injection, and the
"index is consistent with the verified copies" check."""

from __future__ import annotations

import errno
import hashlib
import json
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from sentinel.archive import layout
from sentinel.archive.index import parse_journal
from sentinel.archive.io import ArchiveIO
from sentinel.archive.locator import DriveScan
from sentinel.db import Ledger
from sentinel.export.fileio import PARTIAL
from sentinel.export.target import ExportTarget, RemovalResult


def sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def make_drive(tmp: Path, name: str, marker: bool = False, role: str | None = "archive") -> ExportTarget:
    root = tmp / f"archive_{name}"
    root.mkdir(exist_ok=True)
    duid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"sentinel-test-{name}"))
    if marker:
        m: dict[str, Any] = {"drive_uuid": duid, "label": f"ARCHIVE_{name}"}
        if role:
            m["role"] = role
        (root / "SENTINEL_DRIVE.json").write_text(json.dumps(m), "utf-8")
    return ExportTarget(root, {"drive_uuid": duid, "label": f"ARCHIVE_{name}"})


class FaultyIO(ArchiveIO):
    """A drive that can be unplugged, fill up, fail a write, or store wrong bytes."""

    def __init__(self, *, quota: int | None = None, free: int | None = None, unplug_on_write_file: int | None = None,
                 eio_on_write_file: int | None = None, unplug_on_read_file: int | None = None,
                 corrupt_writes: int = 0, refuse_dir_flush: bool = False, buffered_reads: bool = False) -> None:
        super().__init__()
        self.refuse_dir_flush = refuse_dir_flush
        self.buffered_reads = buffered_reads
        self.dir_flushes = 0
        self.quota, self.free = quota, free
        self.written = 0
        self.unplug_on_write_file = unplug_on_write_file
        self.eio_on_write_file = eio_on_write_file
        self.unplug_on_read_file = unplug_on_read_file
        self.corrupt_writes = corrupt_writes
        self.write_files = 0
        self.read_files = 0
        self.fsyncs = 0
        self.unplugged = False

    def _gone(self) -> None:
        if self.unplugged:
            raise OSError(errno.ENODEV, "The device is not connected")

    def free_bytes(self, root: Path) -> int:
        self._gone()
        if self.free is not None:
            return self.free
        if self.quota is not None:
            return max(0, self.quota - self.written)
        return super().free_bytes(root)

    def root_present(self, root: Path) -> bool:
        return not self.unplugged and super().root_present(root)

    def exists(self, path: Path) -> bool:
        self._gone()
        return super().exists(path)

    def discard_partial(self, dest: Path) -> bool:
        self._gone()              # an unplugged drive cannot be tidied: the .partial stays
        return super().discard_partial(dest)

    def write_new(self, dest: Path, chunks: Iterable[bytes], expected_sha256: str | None = None,
                  fsync_every: int | None = None) -> tuple[str, int]:
        self._gone()
        if not dest.name.endswith(".txt") or dest.name.endswith(layout.FRAMEHASH_SUFFIX):
            self.write_files += 1   # count evidence files only (not README)
        return super().write_new(dest, chunks, expected_sha256, fsync_every)

    def _write(self, fh: Any, data: bytes) -> None:
        self._gone()
        if self.quota is not None and self.written + len(data) > self.quota:
            raise OSError(errno.ENOSPC, "No space left on device")
        name = Path(fh.name).name
        evidence = name.endswith(PARTIAL) and not name.startswith(("archive_index", "gaps", "SHA256SUMS", "README"))
        if evidence and self.unplug_on_write_file is not None and self.write_files == self.unplug_on_write_file:
            fh.write(data[: len(data) // 2])
            fh.flush()
            self.unplugged = True
            raise OSError(errno.EIO, "The device is not connected")
        if evidence and self.eio_on_write_file is not None and self.write_files == self.eio_on_write_file:
            self.eio_on_write_file = None
            fh.write(data[: len(data) // 3])
            raise OSError(errno.EIO, "I/O error")
        if evidence and self.corrupt_writes and name.endswith(".ts" + PARTIAL):
            self.corrupt_writes -= 1
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        self.written += len(data)
        fh.write(data)

    def _fsync(self, fh: Any) -> None:
        self.fsyncs += 1
        super()._fsync(fh)

    def fsync_dir(self, path: Path) -> bool:
        self._gone()
        self.dir_flushes += 1
        if self.refuse_dir_flush:
            self.last_dir_flush = False
            return False
        return super().fsync_dir(path)

    def read_back(self, path: Path) -> tuple[Iterator[bytes], str]:
        self._gone()
        self.read_files += 1
        chunks, method = super().read_back(path)
        if self.buffered_reads:
            from sentinel.export.fileio import M_BUFFERED
            method = M_BUFFERED
        if self.unplug_on_read_file is not None and self.read_files == self.unplug_on_read_file:
            def gen() -> Iterator[bytes]:
                for c in chunks:
                    yield c[:100]
                    self.unplugged = True
                    raise OSError(errno.EIO, "The device is not connected")
            return gen(), method
        return chunks, method

    def append_line(self, path: Path, line: str) -> None:
        self._gone()
        super().append_line(path, line)


class SwitchLocator:
    """Which fake drive is 'inserted'. ``io.unplugged`` makes the drive vanish."""

    def __init__(self, target: ExportTarget | None, io: FaultyIO | None = None) -> None:
        self.target = target
        self.io = io

    def scan(self) -> DriveScan:
        if self.target is None or (self.io is not None and self.io.unplugged):
            return DriveScan()
        if not Path(self.target.root).is_dir():
            return DriveScan()
        return DriveScan([self.target])


class FakeEjector:
    def __init__(self) -> None:
        self.flushed = 0
        self.ejected = 0

    def flush(self, target: ExportTarget) -> None:
        self.flushed += 1

    def eject(self, target: ExportTarget) -> RemovalResult:
        self.ejected += 1
        return RemovalResult(True, "SAFE TO REMOVE", "fake ejector (test)")


def drain(worker: Any, max_steps: int = 500) -> list[str]:
    out = []
    for _ in range(max_steps):
        r = worker.step()
        out.append(r)
        if r != "copied":
            return out
    raise AssertionError(f"did not finish in {max_steps} steps: {out[-10:]}")


def claimed(ledger: Ledger, drive_id: str) -> dict[str, str]:
    return {r["path"]: r["sha256"] for r in ledger.query("SELECT * FROM archive_copies WHERE drive_id=? AND "
                                                          "state='VERIFIED'", (drive_id,))}


def check_drive(root: Path, ledger: Ledger, drive_id: str, complete: bool = True, camera: str = "CAM01") -> dict[str, Any]:
    """The index on the drive never lists anything that is not a verified copy on it; together
    with the journal it documents every verified copy; every claimed file is really there with
    the sealed hash."""
    rows = ledger.query("SELECT a.*, s.sha256 AS sealed, s.framehash_sha256 AS fsealed FROM archive_copies a "
                        "JOIN segments s ON s.segment_id=a.segment_id WHERE a.drive_id=? AND a.state='VERIFIED'", (drive_id,))
    have = {}
    for r in rows:
        p = root / r["path"]
        assert p.is_file(), f"claimed file missing: {r['path']}"
        assert sha_file(p) == r["sha256"], f"claimed file content differs: {r['path']}"
        assert r["sha256"] == (r["fsealed"] if r["kind"] == layout.KIND_FRAMEHASH else r["sealed"])
        assert not r["path"].endswith(PARTIAL)
        have[r["path"]] = r["sha256"]
    names = layout.index_files(camera)
    doc = json.loads((root / names["json"]).read_text("utf-8"))
    assert doc["format"] == "sentinel-archive-index"
    listed = {c["path"]: c["sha256"] for c in doc["copies"]}
    assert listed.items() <= have.items(), "index lists a file that is not a verified copy on this drive"
    sums = {}
    for ln in (root / names["sums"]).read_text("utf-8").splitlines():
        sha, path = ln.split("  ", 1)
        sums[path] = sha
    carried = {c["path"]: c["sha256"] for c in doc.get("carried_forward") or []}
    assert sums == {**listed, **carried}
    assert not set(carried) & set(have)
    csv_lines = (root / names["csv"]).read_text("utf-8").splitlines()
    assert len(csv_lines) == 1 + len(listed) + len(carried)
    journal = {}
    jp = root / names["journal"]
    if jp.exists():
        journal = {e["path"]: e["sha256"] for e in parse_journal(jp.read_bytes())}
    lost = {r["path"] for r in ledger.query("SELECT path FROM archive_copies WHERE drive_id=? AND state='LOST'",
                                            (drive_id,))} - set(have)   # a re-copy re-uses the path
    for path, sha in journal.items():   # history: a journal line is a copy verified at that time
        assert have.get(path) == sha or path in lost or path in carried, f"journal line for unknown copy {path}"
    assert not set(listed) & lost, "index lists a copy marked LOST"
    assert set(have) <= set(listed) | set(journal), "drive does not document every verified copy"
    if complete:
        assert listed == have
    return doc
