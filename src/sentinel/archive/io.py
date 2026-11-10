"""Archive-drive file I/O: reuses the review-export primitives (``sentinel.export.fileio``).

* New evidence files: :meth:`TargetIO.write_new` — ``.partial`` → periodic + final fsync →
  no-replace rename → directory fsync. Never overwrites; a crash/unplug leaves only a ``.partial``.
* Verification: :func:`open_unbuffered` — cache-bypassing read-back (Windows
  ``FILE_FLAG_NO_BUFFERING``; Linux ``O_DIRECT`` after ``fadvise(DONTNEED)``, NOT EXECUTED).
* Index files (the only files ever rewritten): :meth:`ArchiveIO.replace_file` — write a
  ``.partial``, fsync, read it back uncached and compare, then atomically replace the old one.
* Journal: :meth:`ArchiveIO.append_line` — one ``write`` + fsync per verified file.

:class:`Throttle` limits the byte rate of local-recording reads so the copy can never take the
spool disk's bandwidth away from the recorder (the recorder writes ~0.5-1.5 MB/s; the default
limit is 40 MB/s, still ≥ 25x the recording rate).
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from ..export.fileio import CHUNK, PARTIAL, TargetIO, open_unbuffered, partial_path

MIB = 1024 * 1024


class Aborted(Exception):
    """The copy in flight was abandoned on purpose (swap deadline, shutdown). Not a drive error."""


class Throttle:
    """Byte-rate limiter (token bucket with one chunk of burst). ``rate`` in bytes/second."""

    def __init__(self, rate: float, *, mono: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.rate = float(rate)
        self._mono = mono
        self._sleep = sleep
        self._t0: float | None = None
        self._bytes = 0.0
        self.slept_s = 0.0

    def consume(self, n: int) -> None:
        now = self._mono()
        if self._t0 is None or now - (self._t0 + self._bytes / self.rate) > 1.0:
            self._t0, self._bytes = now, 0.0          # idle for a while: no credit is carried over
        self._bytes += n
        due = self._t0 + self._bytes / self.rate
        if due > now:
            self._sleep(due - now)
            self.slept_s += due - now


class ArchiveIO(TargetIO):
    """All file-system access to the archive drive (tests inject faults by subclassing)."""

    def __init__(self, read_chunk: int = 8 * MIB) -> None:
        self.read_chunk = max(CHUNK, read_chunk)
        self.last_dir_flush: bool | None = None     # result of the most recent directory flush

    def fsync_dir(self, path: Path) -> bool:
        self.last_dir_flush = super().fsync_dir(path)
        return self.last_dir_flush

    def confirm_in_place(self, dest: Path) -> None:
        """Before a copy is recorded: make sure its rename is durable. If the file system refused the
        directory flush, re-list the directory (fresh listing, not a cached stat) and require the final
        name to be present and the ``.partial`` gone; then ask for the flush once more."""
        if self.fsync_dir(dest.parent):
            return
        names = set(os.listdir(dest.parent))
        if dest.name not in names or partial_path(dest).name in names:
            raise OSError(f"rename of {dest.name} is not visible in a fresh directory listing")
        self.fsync_dir(dest.parent)

    def read_back(self, path: Path) -> tuple[Iterator[bytes], str]:
        return open_unbuffered(path, self.read_chunk)

    def rename_aside(self, path: Path, suffix: str) -> Path:
        """Move a file that failed verification out of the way (never deleted, never claimed)."""
        new = path.with_name(path.name + suffix)
        n = 1
        while new.exists():
            n += 1
            new = path.with_name(f"{path.name}{suffix}-{n}")
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass
        os.rename(path, new)
        return new

    def replace_file(self, dest: Path, data: bytes, verify: Callable[[Path], str]) -> str:
        """Replace ``dest`` with ``data``; ``verify(path)`` returns the uncached read-back SHA-256. The
        temporary file must verify before the old file is replaced, and the final file is verified again
        afterwards (write-then-verify). ``os.replace`` is atomic on NTFS and ext4; on exFAT/FAT it is NOT
        guaranteed to be atomic — a power cut during the replace can leave the old or no file under the
        final name (the next index rewrite repairs it; the journal still lists every copy)."""
        import hashlib

        self.makedirs(dest.parent)
        tmp = partial_path(dest)
        self.discard_partial(dest)
        expected = hashlib.sha256(data).hexdigest()
        try:
            with open(tmp, "xb") as fh:
                self._write(fh, data)
                fh.flush()
                os.fsync(fh.fileno())
            got = verify(tmp)
            if got != expected:
                raise OSError(f"index file read-back mismatch for {dest.name}")
            for attempt in range(20):
                try:
                    os.replace(tmp, dest)
                    break
                except PermissionError:  # Windows: someone has the old index open
                    if attempt == 19:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except BaseException:
            try:
                self.discard_partial(dest)
            except OSError:
                pass
            raise
        self.fsync_dir(dest.parent)
        if verify(dest) != expected:
            raise OSError(f"index file {dest.name} does not read back correctly after replacing it")
        return expected

    def append_line(self, path: Path, line: str) -> None:
        self.makedirs(path.parent)
        with open(path, "ab") as fh:
            self._write(fh, line.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())

    def restore_partial(self, partial: Path, dest: Path) -> None:
        """Put back a verified copy whose rename was lost (the file system rolled it back to its
        ``.partial`` name after an unclean removal). No-replace rename, then directory flush."""
        self.rename_noreplace(partial, dest)
        self.confirm_in_place(dest)

    def stray_partials(self, root: Path) -> list[Path]:
        """Every ``*.partial`` under ``root`` (left by an unplug / crash; never claimed)."""
        out: list[Path] = []
        if not root.is_dir():
            return out
        for dirpath, _dirs, files in os.walk(root):
            out.extend(Path(dirpath) / f for f in files if f.endswith(PARTIAL))
        return out

    def remove_partial_file(self, path: Path) -> None:
        """Delete one ``.partial`` file (an unverified, never-claimed temporary copy)."""
        if not path.name.endswith(PARTIAL):
            raise ValueError(f"refusing to delete a non-temporary file: {path}")
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass
        path.unlink()
