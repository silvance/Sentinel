"""File operations on the export drive: write-once copies and cache-bypassing read-back.

Write path (every file): ``<name>.partial`` → write → flush + fsync (Windows:
``FlushFileBuffers``) → rename to the final name **without replacing** an existing file →
directory fsync (POSIX). A crash or unplug leaves at most a ``.partial``, which is never
claimed and is discarded on resume.

Read-back (what "verified" means here):

* **Windows** — ``CreateFileW`` with ``FILE_FLAG_NO_BUFFERING`` and sector-aligned buffers
  and transfer sizes (``VirtualAlloc`` memory, chunk = multiple of the volume sector size).
  The bytes come from the storage device, not from the Windows file cache. This is exercised
  by the test suite on the development host (NTFS, internal disk).
* **Linux** — ``posix_fadvise(DONTNEED)`` then ``O_DIRECT`` reads into page-aligned memory;
  falls back to fadvise + buffered read if the filesystem rejects ``O_DIRECT``. NOT EXECUTED
  on the development host.
* If unbuffered access is refused by the filesystem, a normal buffered read is used and the
  method is recorded as ``buffered-cache-not-bypassed`` in the ledger and manifest.

What read-back does NOT prove: a device's own internal cache (disk DRAM / USB bridge) may
still serve the read; nothing in user space can bypass it. The file was flushed with
``FlushFileBuffers``/``fsync`` first, which asks the device to commit its write cache, but a
device that ignores flush commands cannot be detected this way.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import logging
import os
import shutil
import stat
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

log = logging.getLogger(__name__)

PARTIAL = ".partial"
CHUNK = 1024 * 1024

M_WIN_UNBUFFERED = "windows-unbuffered(FILE_FLAG_NO_BUFFERING)"
M_LINUX_DIRECT = "linux-o_direct(after fadvise-dontneed)"
M_POSIX_FADVISE = "posix-fadvise-dontneed+buffered"
M_BUFFERED = "buffered-cache-not-bypassed"

# Windows error codes that mean "this volume is full".
_WIN_DISK_FULL = {39, 112}            # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
# Windows errors that typically mean "the device went away".
WIN_DEVICE_GONE = {2, 3, 15, 21, 55, 1167, 483}


class SourceMismatch(RuntimeError):
    """The local source no longer hashes to its sealed value (it was changed after sealing)."""


def is_disk_full(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    return exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) or getattr(exc, "winerror", None) in _WIN_DISK_FULL


def partial_path(dest: Path) -> Path:
    return dest.with_name(dest.name + PARTIAL)


def sha256_chunks(chunks: Iterable[bytes]) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    for c in chunks:
        h.update(c)
        n += len(c)
    return h.hexdigest(), n


# ------------------------------------------------------------------------------ unbuffered read
def _win_long(path: Path) -> str:
    p = os.path.abspath(path)
    return p if p.startswith("\\\\") else "\\\\?\\" + p


def _read_windows_unbuffered(path: Path, chunk: int) -> Iterator[bytes] | None:
    """Generator over the file read with FILE_FLAG_NO_BUFFERING, or None if the volume refuses it."""
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.ReadFile.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.VirtualAlloc.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    k32.VirtualAlloc.restype = wintypes.LPVOID
    k32.VirtualFree.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
    k32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    k32.GetDiskFreeSpaceW.argtypes = [wintypes.LPCWSTR] + [ctypes.POINTER(wintypes.DWORD)] * 4

    vol = ctypes.create_unicode_buffer(1024)
    sector = 4096
    if k32.GetVolumePathNameW(str(os.path.abspath(path)), vol, len(vol)):
        spc, bps, nfc, tnc = (wintypes.DWORD() for _ in range(4))
        if k32.GetDiskFreeSpaceW(vol.value, ctypes.byref(spc), ctypes.byref(bps), ctypes.byref(nfc),
                                 ctypes.byref(tnc)) and bps.value:
            sector = max(sector, int(bps.value))
    size = max(sector, (chunk // sector) * sector)

    GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING = 0x80000000, 0x1, 3
    FILE_FLAG_NO_BUFFERING = 0x20000000
    h = k32.CreateFileW(_win_long(path), GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING,
                        FILE_FLAG_NO_BUFFERING, None)
    if h is None or h == ctypes.c_void_p(-1).value:
        err = ctypes.get_last_error()
        if err in (50, 87):  # ERROR_NOT_SUPPORTED, ERROR_INVALID_PARAMETER: volume refuses unbuffered I/O
            return None
        raise ctypes.WinError(err)

    def gen() -> Iterator[bytes]:
        buf = k32.VirtualAlloc(None, size, 0x3000, 0x04)  # MEM_COMMIT|MEM_RESERVE, PAGE_READWRITE
        if not buf:
            k32.CloseHandle(h)
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            n = wintypes.DWORD()
            while True:
                if not k32.ReadFile(h, buf, size, ctypes.byref(n), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if n.value == 0:
                    return
                yield ctypes.string_at(buf, n.value)
        finally:
            k32.VirtualFree(buf, 0, 0x8000)  # MEM_RELEASE
            k32.CloseHandle(h)

    return gen()


def _read_posix_direct(path: Path, chunk: int) -> tuple[Iterator[bytes], str]:
    # NOT EXECUTED on the development host.
    import mmap

    fd = os.open(path, os.O_RDONLY)
    try:
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)
    o_direct = getattr(os, "O_DIRECT", 0)
    if o_direct:
        try:
            dfd = os.open(path, os.O_RDONLY | o_direct)
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise
        else:
            def gen() -> Iterator[bytes]:
                buf = mmap.mmap(-1, chunk)
                try:
                    while True:
                        n = os.readv(dfd, [buf])
                        if n == 0:
                            return
                        yield bytes(buf[:n])
                finally:
                    buf.close()
                    os.close(dfd)
            return gen(), M_LINUX_DIRECT
    return _read_buffered(path, chunk), (M_POSIX_FADVISE if hasattr(os, "posix_fadvise") else M_BUFFERED)


def _read_buffered(path: Path, chunk: int) -> Iterator[bytes]:
    with open(path, "rb", buffering=0) as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                return
            yield b


def open_unbuffered(path: Path, chunk: int = CHUNK) -> tuple[Iterator[bytes], str]:
    """Return (chunk iterator, method). The method string is recorded with every verification."""
    if sys.platform == "win32":
        g = _read_windows_unbuffered(path, chunk)
        if g is not None:
            return g, M_WIN_UNBUFFERED
        log.warning("volume refused unbuffered reads for %s; verification uses the OS cache", path)
        return _read_buffered(path, chunk), M_BUFFERED
    return _read_posix_direct(path, chunk)


# ------------------------------------------------------------------------------ target I/O
class TargetIO:
    """All file-system access to the export drive goes through here (tests inject faults)."""

    def free_bytes(self, root: Path) -> int:
        return shutil.disk_usage(root).free

    def root_present(self, root: Path) -> bool:
        try:
            os.listdir(root)
            return True
        except OSError:
            return False

    def exists(self, path: Path) -> bool:
        return path.exists()

    def makedirs(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def discard_partial(self, dest: Path) -> bool:
        p = partial_path(dest)
        try:
            os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
            p.unlink()
            return True
        except FileNotFoundError:
            return False

    def _write(self, fh: object, data: bytes) -> None:
        fh.write(data)  # type: ignore[attr-defined]

    def _fsync(self, fh: object) -> None:
        """Periodic flush during write_new (separate so tests can count / fail it)."""
        os.fsync(fh.fileno())  # type: ignore[attr-defined]

    def write_new(self, dest: Path, chunks: Iterable[bytes], expected_sha256: str | None = None,
                  fsync_every: int | None = None) -> tuple[str, int]:
        """Write ``chunks`` as ``dest`` via .partial/fsync/no-replace rename. Returns (sha256 of
        the bytes written, size). Raises FileExistsError if ``dest`` already exists, and
        :class:`SourceMismatch` (nothing renamed) if the bytes do not hash to ``expected_sha256``.
        ``fsync_every``: additionally flush to the device after at least this many bytes (the
        archive copier uses it so a slow drive never holds a large amount of dirty cache)."""
        self.makedirs(dest.parent)
        tmp = partial_path(dest)
        self.discard_partial(dest)
        h = hashlib.sha256()
        n = 0
        unsynced = 0
        try:
            with open(tmp, "xb") as fh:
                for c in chunks:
                    self._write(fh, c)
                    h.update(c)
                    n += len(c)
                    unsynced += len(c)
                    if fsync_every and unsynced >= fsync_every:
                        fh.flush()
                        self._fsync(fh)
                        unsynced = 0
                fh.flush()
                os.fsync(fh.fileno())
            if expected_sha256 is not None and h.hexdigest() != expected_sha256:
                raise SourceMismatch(f"source bytes for {dest.name} do not match the sealed SHA-256")
            self.rename_noreplace(tmp, dest)
        except BaseException:
            try:
                self.discard_partial(dest)
            except OSError:
                pass
            raise
        self.confirm_in_place(dest)
        return h.hexdigest(), n

    def confirm_in_place(self, dest: Path) -> None:
        """Make a just-renamed file's directory entry durable (integration: the archive branch's
        directory-flush fix, now used by the export write path too). If the file system refuses
        the directory flush, require the final name present and the ``.partial`` gone in a fresh
        listing, then ask for the flush once more; otherwise raise OSError (the item fails and is
        retried; nothing is reported verified)."""
        if self.fsync_dir(dest.parent):
            return
        names = set(os.listdir(dest.parent))
        if dest.name not in names or partial_path(dest).name in names:
            raise OSError(f"rename of {dest.name} is not visible in a fresh directory listing")
        self.fsync_dir(dest.parent)

    def finish_external(self, dest: Path) -> int:
        """A file written by another program (FFmpeg) at ``partial_path(dest)``: fsync, rename."""
        tmp = partial_path(dest)
        self.check_external(tmp)
        fd = os.open(tmp, os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        size = tmp.stat().st_size
        self.rename_noreplace(tmp, dest)
        self.confirm_in_place(dest)
        return size

    def check_external(self, path: Path) -> None:
        """Hook for fault injection (quota checks on files written by FFmpeg)."""

    def rename_noreplace(self, src: Path, dst: Path) -> None:
        if sys.platform == "win32":
            os.rename(src, dst)          # MoveFileEx without REPLACE_EXISTING: fails if dst exists
            return
        try:                             # NOT EXECUTED on the development host
            os.link(src, dst)            # atomic, fails with EEXIST
            os.unlink(src)
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK):
                raise
            # Filesystem without hard links (exFAT/FAT): check-then-rename. Export holds the
            # export lock, so no other Sentinel process writes this drive concurrently.
            if os.path.lexists(dst):
                raise FileExistsError(errno.EEXIST, "destination exists", str(dst)) from None
            os.rename(src, dst)

    def fsync_dir(self, path: Path) -> bool:
        """Commit the directory entry of a just-renamed file. True if the device was asked to commit
        it; False if the file system refused (callers that must be sure then re-list the directory).
        Windows: FlushFileBuffers on a directory handle (``platform.windows.flush_directory``)."""
        if sys.platform == "win32":
            from ..platform.windows import flush_directory

            return flush_directory(path)
        fd = os.open(path, os.O_RDONLY)  # NOT EXECUTED on the development host
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return True

    def read_back(self, path: Path) -> tuple[Iterator[bytes], str]:
        return open_unbuffered(path)

    def seal_read_only(self, path: Path) -> None:
        try:
            os.chmod(path, stat.S_IREAD)
        except OSError as exc:  # some removable filesystems ignore/deny attribute changes
            log.info("could not mark %s read-only: %s", path, exc)
