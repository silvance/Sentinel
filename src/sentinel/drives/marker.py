"""``SENTINEL_DRIVE.json`` (the marker that makes a drive READY), root inspection, and the
write/read-back test performed after preparing.

Read-back must not be satisfied from the OS cache (risk C4). On Windows the test file is re-read
with ``FILE_FLAG_NO_BUFFERING`` (sector-aligned buffer from VirtualAlloc) — exercised on the
development host's own disk against a temporary file. On Linux the page cache is dropped for the
file with ``posix_fadvise(DONTNEED)`` and it is re-read with ``O_DIRECT`` — NOT EXECUTED.
If an uncached read is impossible the result says ``uncached: false``; it is never claimed.
"""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MARKER_NAME = "SENTINEL_DRIVE.json"
MARKER_FORMAT = "sentinel-drive"
MARKER_VERSION = 1
WRITE_TEST_NAME = "SENTINEL_WRITE_TEST.bin"
WRITE_TEST_BYTES = 1024 * 1024  # multiple of every sector size in use (512 / 4096)

# Names that the OS (not a person) creates at the root of a volume. Anything else is "files".
IGNORABLE_ROOT_ENTRIES = frozenset({
    "system volume information", "$recycle.bin", "recycler", "lost+found", ".trashes", ".fseventsd",
    ".spotlight-v100", ".trash-1000", ".trash-0",
})
_REQUIRED = ("format", "version", "drive_uuid", "label", "prepared_utc", "prepared_by")


@dataclass(frozen=True)
class RootContents:
    readable: bool
    entries: tuple[str, ...] = ()           # user-visible names at the root (ignorable ones removed)
    marker: dict[str, Any] | None = None    # valid marker, if present
    marker_error: str | None = None         # marker present but invalid
    error: str | None = None
    sentinel_signs: tuple[str, ...] = ()    # evidence that Sentinel wrote this volume (see sentinel_signs())


def validate_marker(data: Any) -> str | None:
    if not isinstance(data, dict):
        return "not a JSON object"
    missing = [k for k in _REQUIRED if not data.get(k)]
    if missing:
        return f"missing {', '.join(missing)}"
    if data["format"] != MARKER_FORMAT:
        return "wrong format tag"
    if data["version"] != MARKER_VERSION:
        return f"unsupported version {data['version']!r}"
    try:
        uuid.UUID(str(data["drive_uuid"]))
    except ValueError:
        return "drive_uuid is not a UUID"
    return None


def inspect_root(root: str | Path) -> RootContents:
    root = Path(root)
    try:
        with os.scandir(root) as it:
            names = [e.name for e in it]
    except OSError as exc:
        return RootContents(readable=False, error=f"{type(exc).__name__}: {exc.strerror or exc}")
    entries = tuple(sorted(n for n in names if n.lower() not in IGNORABLE_ROOT_ENTRIES))
    marker = None
    marker_error = None
    real = next((n for n in names if n.lower() == MARKER_NAME.lower()), None)
    if real is not None:
        try:
            data = json.loads((root / real).read_text(encoding="utf-8"))
            marker_error = validate_marker(data)
            marker = data if marker_error is None else None
        except (OSError, ValueError) as exc:
            marker_error = f"unreadable: {exc}"
    return RootContents(readable=True, entries=entries, marker=marker, marker_error=marker_error,
                        sentinel_signs=sentinel_signs(root, names))


# Layouts written by Sentinel (docs/04 §2 archive drive, §6 review drive / export branch). Matching is
# deliberately generous: a false positive only means "not erased by this tool, call support".
REVIEW_DIRS = ("recordings", "originals", "manifests", "reports", "quarantine")


def sentinel_signs(root: Path, names: list[str]) -> tuple[str, ...]:
    low = {n.lower(): n for n in names}
    signs = []
    if MARKER_NAME.lower() in low:
        signs.append("marker")
    if (MARKER_NAME + ".partial").lower() in low:
        signs.append("marker_partial")
    if "sentinel" in low and (root / low["sentinel"]).is_dir():
        signs.append("archive_layout")          # Sentinel/INDEX/, Sentinel/<camera>/<date>/...
    if "manifests" in low:
        m = root / low["manifests"]
        try:
            if m.is_dir() and any(e.name.lower() in ("export_manifest.json", "sha256sums.txt") for e in os.scandir(m)):
                signs.append("export_manifest")
        except OSError:
            signs.append("export_manifest_unreadable")
    review = [d for d in REVIEW_DIRS if d in low]
    if "readme_first.txt" in low and review:
        signs.append("review_layout")
    elif len(review) >= 2:
        signs.append("review_layout")
    return tuple(signs)


def inspect_volume(volume: Any) -> RootContents:
    """Default inspector: try each access path of the volume (letter first, then GUID path)."""
    paths = sorted(volume.mount_paths, key=lambda p: (p.startswith("\\\\?\\"), p))
    if not paths:
        return RootContents(readable=False, error="volume is not mounted")
    last = RootContents(readable=False, error="no access path")
    for p in paths:
        last = inspect_root(p)
        if last.readable:
            return last
    return last


# ---------------------------------------------------------------------------- uncached read-back

def _read_uncached_windows(path: Path) -> bytes:
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x1
    OPEN_EXISTING = 3
    FILE_FLAG_NO_BUFFERING = 0x20000000
    FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
    MEM_COMMIT_RESERVE = 0x3000
    MEM_RELEASE = 0x8000
    PAGE_READWRITE = 0x04
    INVALID = ctypes.c_void_p(-1).value

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.ReadFile.restype = wintypes.BOOL
    k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    k32.VirtualAlloc.restype = ctypes.c_void_p
    k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    h = k32.CreateFileW(str(path), GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING,
                        FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN, None)
    if h is None or h == INVALID:
        raise OSError(ctypes.get_last_error(), "CreateFileW(FILE_FLAG_NO_BUFFERING) failed", str(path))
    chunk = 1024 * 1024
    buf = k32.VirtualAlloc(None, chunk, MEM_COMMIT_RESERVE, PAGE_READWRITE)  # page-aligned
    if not buf:
        k32.CloseHandle(h)
        raise OSError(ctypes.get_last_error(), "VirtualAlloc failed")
    out = bytearray()
    try:
        got = wintypes.DWORD(0)
        while True:
            if not k32.ReadFile(h, buf, chunk, ctypes.byref(got), None):
                raise OSError(ctypes.get_last_error(), "ReadFile (unbuffered) failed", str(path))
            if got.value == 0:
                break
            out += ctypes.string_at(buf, got.value)
            if got.value < chunk:
                break
    finally:
        k32.VirtualFree(buf, 0, MEM_RELEASE)
        k32.CloseHandle(h)
    return bytes(out)


def _read_uncached_linux(path: Path) -> bytes:  # NOT EXECUTED (no Linux host in this environment)
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)  # type: ignore[attr-defined]
    finally:
        os.close(fd)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECT)  # type: ignore[attr-defined]
    out = bytearray()
    buf = mmap.mmap(-1, 1024 * 1024)  # page-aligned
    try:
        while True:
            n = os.readv(fd, [buf])
            if n <= 0:
                break
            out += buf[:n]
            if n < len(buf):
                break
    finally:
        buf.close()
        os.close(fd)
    return bytes(out)


def read_uncached(path: str | Path) -> bytes:
    path = Path(path)
    if sys.platform == "win32":
        return _read_uncached_windows(path)
    return _read_uncached_linux(path)


def write_read_back_test(root: str | Path, nbytes: int = WRITE_TEST_BYTES) -> dict[str, Any]:
    """Write random bytes, fsync, re-read bypassing the cache, compare, then remove the test file.

    Raises OSError / ValueError on any failure. Returns a summary for the marker and audit event.
    """
    root = Path(root)
    target = root / WRITE_TEST_NAME
    data = os.urandom(nbytes)
    expected = hashlib.sha256(data).hexdigest()
    with open(target, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    uncached_error = None
    try:
        back = read_uncached(target)
    except (OSError, AttributeError) as exc:
        uncached_error = str(exc)
        back = target.read_bytes()
    got = hashlib.sha256(back).hexdigest()
    target.unlink()
    if got != expected:
        raise ValueError(f"write test read back different data ({got} != {expected})")
    out: dict[str, Any] = {"bytes": nbytes, "sha256": expected, "uncached": uncached_error is None}
    if uncached_error:
        out["uncached_error"] = uncached_error
    return out


def write_marker(root: str | Path, marker: dict[str, Any]) -> dict[str, Any]:
    """Write SENTINEL_DRIVE.json via a temporary name + rename, fsync, and read it back.

    Refuses to overwrite an existing marker (a READY drive is never re-labelled silently).
    """
    err = validate_marker(marker)
    if err:
        raise ValueError(f"invalid marker: {err}")
    root = Path(root)
    final = root / MARKER_NAME
    if final.exists():
        raise FileExistsError(f"{MARKER_NAME} already exists on this drive")
    tmp = root / (MARKER_NAME + ".partial")
    blob = (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    back = json.loads(final.read_bytes().decode("utf-8"))
    if back != marker:
        raise ValueError("marker read back differs from what was written")
    return back
