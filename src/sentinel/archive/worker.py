"""Continuous archive copy to an attached archive drive (requirement S1; docs/04 §2-3).

Mechanism (design choice, see docs/branches/design-archive-drive.md):

* Recording always lands on the internal storage ("spool"); this worker copies every sealed
  segment (FINALIZED / INTERRUPTED; CORRUPT into ``Quarantine/``) and its per-frame sidecar,
  **oldest first**, to whichever archive drive is attached. If the drive is slow, absent or full,
  the footage simply waits on internal storage ("Not yet archived: N h").
* Each file: 8 MiB chunks read from the spool through a byte-rate :class:`Throttle`, written to
  ``<name>.partial`` with an fsync every 64 MiB, fsync, no-replace rename; then read back from
  the drive bypassing the OS cache and compared with the SHA-256 recorded at sealing. Only then
  is it recorded in ``archive_copies``, appended to ``Sentinel/INDEX/verified_journal.csv``, and
  counted as archived. Nothing on the local spool is ever modified or deleted.
* ``Sentinel/INDEX/`` is rewritten atomically on attach, after each ``index_every_s`` of copied
  footage (default one hour; also after an hour of wall time with new copies) and on swap.
* Swap: :meth:`ArchiveWorker.request_swap` (or ``python -m sentinel.archive swap``) → the file in
  flight is finished and verified if that takes at most ``swap_max_wait_s``, otherwise abandoned
  (its ``.partial`` is discarded, never claimed) → index written and read back → flush → eject via
  the :class:`~sentinel.export.target.SafeRemover` (fallback: operator uses the OS "safely remove")
  → SAFE_TO_REMOVE / REMOVE_MANUALLY. The swapped drive is not written again until it has been
  removed (or the operator resumes).
* Unexpected removal: the copy in flight fails, nothing is recorded, the ``.partial`` stays on
  the drive and is discarded when the drive returns; copying resumes with the oldest segment not
  yet verified on the inserted drive (``resume_policy = this_drive``) or on any archive drive
  (``any_drive``).

Runs as its own process in production (``python -m sentinel.archive run``) — see
:mod:`sentinel.archive.__main__` for why — and as a thread in tests (:meth:`start`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .. import __version__
from ..alerts import Alert
from ..clock import compact_utc, iso_utc, parse_iso_utc
from ..config import AppConfig
from ..db import Ledger
from ..export.fileio import PARTIAL, SourceMismatch, is_disk_full
from ..export.ledger import ExportLedger
from ..export.target import ExportTarget, ManualRemovalFallback, SafeRemover, TargetError, same_volume
from ..platform import get_platform
from ..platform.base import InstanceLockError
from . import index as idx
from . import layout
from .io import MIB, Aborted, ArchiveIO, Throttle
from .ledger import ArchiveLedger
from .locator import ConfiguredPathLocator, DriveLocator, DriveUnusable
from .priority import lower_priority
from .status import (ST_ABSENT, ST_ATTENTION, ST_COPYING, ST_FULL, ST_REMOVE_MANUALLY, ST_SAFE, ST_STARTING,
                     ST_STOPPED, ST_SWAP_PENDING, ST_UNUSABLE, ST_UP_TO_DATE, STATUS_FILE, ArchiveStatusWriter,
                     Condition, backlog_text, evaluate_alerts)

log = logging.getLogger(__name__)

CONTROL_DIR = "archive_control"
SWAP_REQUEST = "swap.request"
RESUME_REQUEST = "resume.request"
PROBE = ".sentinel_archive_probe" + PARTIAL
SOURCE_RETRY_S = 3600.0          # retry a local recording that could not be read after this long


class LocalReadError(OSError):
    """Reading a recording on the local spool failed (not an archive-drive problem)."""


class VerifyMismatch(Exception):
    pass


class DriveFull(Exception):
    pass


@dataclass
class Attached:
    target: ExportTarget
    drive_id: str
    last_index_mono: float
    footage_since_index: float = 0.0
    copies_since_index: int = 0
    copies_this_attach: int = 0
    stale_index: bool = False       # index write failed; retry


@dataclass
class Stats:
    files: int = 0
    bytes: int = 0
    copy_s: float = 0.0
    verify_s: float = 0.0
    last: dict[str, Any] = field(default_factory=dict)


class ArchiveWorker:
    def __init__(self, cfg: AppConfig, *, ledger: Ledger | None = None, locator: DriveLocator | None = None,
                 io: ArchiveIO | None = None, remover: SafeRemover | None = None, status_path: Path | None = None,
                 allow_same_volume: bool = False, mono: Callable[[], float] = time.monotonic,
                 lower_own_priority: bool = True) -> None:
        self.cfg = cfg
        self.acfg = cfg.archive
        self._own_ledger = ledger is None
        self.ledger = ledger
        self.locator: DriveLocator = locator or ConfiguredPathLocator(self.acfg.drive_path,
                                                                      self.acfg.require_drive_marker)
        self.io = io or ArchiveIO(read_chunk=self.acfg.chunk_mib * MIB)
        self.remover: SafeRemover = remover or ManualRemovalFallback()
        self.status = ArchiveStatusWriter(status_path or (cfg.data_path / STATUS_FILE))
        self.allow_same_volume = allow_same_volume
        self.mono = mono
        self.lower_own_priority = lower_own_priority
        self.tz = ZoneInfo(cfg.recording.display_timezone)
        self.camera_id = cfg.camera.id
        self.control_dir = cfg.data_path / CONTROL_DIR
        self.chunk = self.acfg.chunk_mib * MIB
        self.fsync_bytes = self.acfg.fsync_every_mib * MIB
        self.reserve = int(self.acfg.reserve_free_mb * 1e6)
        rate = self.acfg.max_rate_mb_s * 1e6
        self.read_throttle = Throttle(rate)
        self.verify_throttle = Throttle(2 * rate)
        self.al: ArchiveLedger | None = None
        self.att: Attached | None = None
        self.state = ST_STARTING
        self.message = "Archive copying is starting."
        self.priority = "unchanged"
        self.stats = Stats()
        # conditions shown to the operator
        self.removed_unexpectedly = False
        self.after_swap = False
        self.full = False
        self.unusable_reason: str | None = None
        self.verify_failed: dict[str, str] = {}     # "seg|drive|kind" -> detail (alert A-006)
        self.source_failed: dict[str, float] = {}   # segment_id -> mono time of failure (alert A-008)
        self.missing_on_drive: list[str] = []
        self.errors = 0                             # consecutive unexpected errors (alert A-007 at >= 3)
        self._verify_attempts: dict[str, int] = {}
        self._skip: set[str] = set()                # segment ids skipped on the current drive
        # control
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._swap = threading.Event()
        self._swap_deadline: float | None = None
        self._swapped_drive: str | None = None
        self._swap_result: tuple[bool, str] | None = None
        self._thread: threading.Thread | None = None
        self._last_publish = 0.0

    # ================================================================== public control
    def request_swap(self) -> None:
        """Operator pressed "Swap Archive Drive": pause at the next segment boundary, then make the
        drive safe to remove. Recording is not affected."""
        if not self._swap.is_set():
            self._swap_deadline = self.mono() + self.acfg.swap_max_wait_s
            self._swap.set()
        self._wake.set()

    def resume(self) -> None:
        """Cancel a pending swap, or continue on a drive that was made safe to remove but not removed."""
        self._swap.clear()
        self._swap_deadline = None
        self._swapped_drive = None
        self._wake.set()

    def stop(self, timeout: float = 60.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def start(self) -> ArchiveWorker:
        """Run in a background thread (tests; production uses the separate process)."""
        self._thread = threading.Thread(target=self.run, kwargs={"whole_process": False}, name="archive",
                                        daemon=True)
        self._thread.start()
        return self

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ================================================================== main loop
    def run(self, whole_process: bool = True) -> None:
        if self.lower_own_priority:
            self.priority = lower_priority(whole_process)
        try:
            lock = get_platform().acquire_instance_lock(self.cfg.data_path / "archive.lock")
        except InstanceLockError:
            log.error("another archive worker is running")
            self.status.write(state=ST_ATTENTION, message="Another archive copy is already running.", alerts=[])
            return
        try:
            self._open()
            self._publish(force=True)
            while not self._stop.is_set():
                try:
                    outcome = self.step()
                    self.errors = 0
                except Exception as exc:  # never die silently: report and retry
                    self.errors += 1
                    log.exception("archive step failed (%d consecutive)", self.errors)
                    self._event("error", "archive_error", "Archive copy error", {"error": f"{type(exc).__name__}: {exc}"})
                    self._set(ST_ATTENTION, "Archive copying had a problem and will retry automatically.")
                    outcome = "error"
                if outcome != "copied":
                    self._wake.wait(self.acfg.poll_interval_s if outcome != "error" else
                                    min(60.0, self.acfg.poll_interval_s * (2 ** min(self.errors, 4))))
                    self._wake.clear()
        finally:
            try:
                self._set(ST_STOPPED, "Archive copying stopped.")
                self._publish(force=True)
            except Exception:
                log.exception("could not write final archive status")
            if self._own_ledger and self.ledger is not None:
                self.ledger.close()
                self.ledger = None
            lock.close()

    def _open(self) -> None:
        if self.ledger is None:
            self.ledger = Ledger(self.cfg.data_path / "sentinel.db")
        if self.al is None:
            self.al = ArchiveLedger(self.ledger, self.camera_id, self.acfg.resume_policy)

    def step(self) -> str:
        """One unit of work: look at the drive, then copy+verify at most one segment. Returns
        'absent' | 'unusable' | 'waiting_for_removal' | 'swapped' | 'full' | 'idle' | 'copied'."""
        self._open()
        self._poll_control()
        try:
            target = self.locator.locate()
            self.unusable_reason = None
        except DriveUnusable as exc:
            target = None
            self.unusable_reason = str(exc)
        if target is not None:
            try:
                key = target.drive_key
            except TargetError as exc:
                target, self.unusable_reason = None, str(exc)
        if target is None:
            return self._no_drive()
        if self._swapped_drive == key:
            self._waiting_for_removal()
            return "waiting_for_removal"
        if self.att is None or self.att.drive_id != key:
            if self.att is not None:
                self._detached(unexpected=True)
            if not self._attach(target):
                return "unusable"
        assert self.att is not None
        if self._swap.is_set():
            return self._do_swap()
        if self.att.stale_index:
            self._write_index("retry")
        if self.full and not self._space_available():
            self._set(ST_FULL, "The archive drive is full. Replace it with an empty archive drive. "
                               "Recording continues.")
            return "full"
        self.full = False
        self._expire_source_failures()
        pending = self.al.pending_segments(self.att.drive_id, limit=1,  # type: ignore[union-attr]
                                           exclude=frozenset(self._skip | set(self.source_failed)))
        if not pending:
            if self.att.copies_since_index and self.mono() - self.att.last_index_mono >= self.acfg.index_every_s:
                self._write_index("hourly")
            if self._skip or self.source_failed:
                self._set(ST_ATTENTION, "Some recordings could not be copied to the archive drive. "
                                        "Recording continues.")
            else:
                self._set(ST_UP_TO_DATE, "Archive drive connected. All recordings are copied. Recording continues.")
            return "idle"
        self._set(ST_COPYING, "Copying recordings to the archive drive. Recording continues.")
        self._archive_segment(pending[0])
        if self.att is not None and (self.att.footage_since_index >= self.acfg.index_every_s
                                     or (self.att.copies_since_index
                                         and self.mono() - self.att.last_index_mono >= self.acfg.index_every_s)):
            self._write_index("hourly")
        return "copied"

    # ================================================================== drive presence
    def _no_drive(self) -> str:
        if self.att is not None:
            self._detached(unexpected=True)
        self._swapped_drive = None            # the swapped drive has been taken out
        if self._swap.is_set():               # nothing left to prepare
            self._clear_swap()
        if self.unusable_reason:
            self._set(ST_UNUSABLE, "This drive cannot be used as an archive drive. Recording continues.")
            return "unusable"
        if self.after_swap:
            self._set(ST_ABSENT, "Insert the next archive drive. Recording continues.")
        elif self.removed_unexpectedly:
            self._set(ST_ABSENT, "The archive drive was removed without preparing it. No recordings were lost; "
                                 "copying will continue when a drive is inserted. Recording continues.")
        else:
            self._set(ST_ABSENT, "No archive drive is connected. Recording continues.")
        return "absent"

    def _detached(self, unexpected: bool) -> None:
        att = self.att
        self.att = None
        self.full = False
        self._skip.clear()
        self.verify_failed.clear()
        self.missing_on_drive = []
        if att is None:
            return
        if unexpected:
            self.removed_unexpectedly = True
            self._event("warning", "archive_drive_removed", "Archive drive removed without preparing it",
                        {"drive_id": att.drive_id, "copies_this_attach": att.copies_this_attach})
            log.warning("archive drive %s removed without preparing", att.drive_id)

    def _attach(self, target: ExportTarget) -> bool:
        root = Path(target.root)
        if not self.io.root_present(root):
            return False
        if not self.allow_same_volume and same_volume(root, self.cfg.spool_path):
            self.unusable_reason = "the archive drive is the recording storage itself"
            self._set(ST_UNUSABLE, "This drive cannot be used as an archive drive. Recording continues.")
            return False
        full = False
        try:
            self.io.makedirs(root / layout.INDEX_DIR)
            probe = root / layout.ROOT / PROBE
            with open(probe, "wb") as fh:
                fh.write(b"\0" * 4096)
                fh.flush()
                os.fsync(fh.fileno())
            probe.unlink()
        except OSError as exc:
            if is_disk_full(exc):
                full = True
            else:
                self.unusable_reason = f"write test failed: {type(exc).__name__}: {exc}"
                self._event("warning", "archive_drive_unusable", "Archive drive cannot be written",
                            {"error": self.unusable_reason, "identity": target.identity})
                self._set(ST_UNUSABLE, "This drive cannot be used as an archive drive. Recording continues.")
                return False
        drive_id = ExportLedger(self.ledger).upsert_drive(target)  # type: ignore[arg-type]
        self.att = Attached(target=target, drive_id=drive_id, last_index_mono=self.mono())
        self.removed_unexpectedly = self.after_swap = False
        self.unusable_reason = None
        self.full = full
        self._skip.clear()
        self.verify_failed.clear()
        self._verify_attempts.clear()
        removed = 0
        for p in self.io.stray_partials(root / layout.ROOT):   # unverified leftovers: never claimed
            try:
                self.io.remove_partial_file(p)
                removed += 1
            except OSError as exc:
                log.warning("could not discard %s: %s", p, exc)
        self.missing_on_drive = [r["path"] for r in self.al.copies_on(drive_id)  # type: ignore[union-attr]
                                 if not (root / r["path"]).is_file()]
        if self.missing_on_drive:
            self._event("error", "archive_files_missing", "Files recorded as archived are missing from the drive",
                        {"drive_id": drive_id, "missing": self.missing_on_drive[:50],
                         "count": len(self.missing_on_drive)})
        q = self.al.backlog(drive_id)  # type: ignore[union-attr]
        self._event("info", "archive_drive_attached", "Archive drive connected",
                    {"drive_id": drive_id, "label": target.label, "discarded_partials": removed,
                     "to_copy_segments": q.segments, "to_copy_seconds": round(q.seconds, 1), "full": full})
        log.info("archive drive %s attached (%d partial(s) discarded, %d segment(s) to copy)", drive_id, removed,
                 q.segments)
        if not (root / layout.README).exists():
            try:
                self.io.write_new(root / layout.README, [idx.README_TEXT.encode("utf-8")])
            except FileExistsError:
                pass
            except OSError as exc:
                if not self._drive_error(exc):
                    raise
                return self.att is not None
        self._write_index("attach")
        return self.att is not None

    def _space_available(self, need: int = 0) -> bool:
        assert self.att is not None
        try:
            free = self.io.free_bytes(Path(self.att.target.root))
        except OSError:
            return False
        if need == 0:
            nxt = self.al.pending_segments(self.att.drive_id, limit=1)  # type: ignore[union-attr]
            need = int(nxt[0]["size_bytes"] or 0) if nxt else 0
        return free >= need + self.reserve

    # ================================================================== swap
    def _poll_control(self) -> None:
        for name, action in ((SWAP_REQUEST, self.request_swap), (RESUME_REQUEST, self.resume)):
            p = self.control_dir / name
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    continue
                self._event("audit", "archive_" + name.split(".")[0] + "_requested",
                            f"Archive {name.split('.')[0]} requested", None)
                action()

    def _clear_swap(self) -> None:
        self._swap.clear()
        self._swap_deadline = None

    def _do_swap(self) -> str:
        att = self.att
        assert att is not None
        self._set(ST_SWAP_PENDING, "Preparing the archive drive for removal. Do not remove it yet. Recording continues.")
        self._publish(force=True)
        root = Path(att.target.root)
        for p in self.io.stray_partials(root / layout.ROOT):
            self.io.remove_partial_file(p)
        if not self._write_index("swap"):
            if self.att is None:          # drive vanished while preparing: reported as removed
                self._clear_swap()
                return "absent"
            raise RuntimeError("the archive index could not be written for the swap")
        self.remover.flush(att.target)
        res = self.remover.eject(att.target)
        self._clear_swap()
        self._swapped_drive = att.drive_id
        self.after_swap = True
        self.att = None
        self._swap_result = (res.ejected, res.detail)
        self._event("audit", "archive_swap_ready", "Archive drive prepared for removal",
                    {"drive_id": att.drive_id, "ejected": res.ejected, "detail": res.detail})
        self._waiting_for_removal()
        return "swapped"

    def _waiting_for_removal(self) -> None:
        ejected = bool(self._swap_result and self._swap_result[0])
        if ejected:
            self._set(ST_SAFE, "SAFE TO REMOVE ARCHIVE DRIVE. Recording continues.")
        elif os.name == "nt":
            self._set(ST_REMOVE_MANUALLY, "The archive drive is ready to be removed. Before unplugging it, click the "
                                          "\"Safely Remove Hardware\" icon near the clock, choose this drive, and wait "
                                          "for the message that it is safe to remove. Recording continues.")
        else:
            self._set(ST_REMOVE_MANUALLY, "The archive drive is ready to be removed. Before unplugging it, use the "
                                          "\"Eject\" or \"Safely remove\" option for this drive and wait until it "
                                          "disappears. Recording continues.")

    # ================================================================== copying
    def _expire_source_failures(self) -> None:
        now = self.mono()
        for sid, t in list(self.source_failed.items()):
            if now - t >= SOURCE_RETRY_S:
                del self.source_failed[sid]

    def _archive_segment(self, seg: Any) -> None:
        assert self.att is not None and self.al is not None
        kinds = []
        mk = layout.media_kind(seg["state"])
        if not self.al.has_copy(seg["segment_id"], self.att.drive_id, mk):
            kinds.append(mk)
        if (seg["framehash_rel_path"] and seg["framehash_sha256"]
                and not self.al.has_copy(seg["segment_id"], self.att.drive_id, layout.KIND_FRAMEHASH)):
            kinds.append(layout.KIND_FRAMEHASH)
        for kind in kinds:
            if self._stop.is_set() or self.att is None:
                return
            try:
                self._archive_file(seg, kind)
            except Aborted:
                log.info("archive copy of %s abandoned (%s)", seg["rel_path"], "stop" if self._stop.is_set() else "swap")
                return
            except (SourceMismatch, LocalReadError) as exc:
                self.source_failed[seg["segment_id"]] = self.mono()
                self._event("error", "archive_source_unreadable", "A local recording could not be read for archiving",
                            {"segment_id": seg["segment_id"], "kind": kind, "error": f"{type(exc).__name__}: {exc}"})
                return
            except VerifyMismatch as exc:
                key = f"{seg['segment_id']}|{self.att.drive_id if self.att else ''}|{kind}"
                n = self._verify_attempts.get(key, 0) + 1
                self._verify_attempts[key] = n
                self._event("error", "archive_verify_failed", "An archive copy did not verify",
                            {"segment_id": seg["segment_id"], "kind": kind, "attempt": n, "detail": str(exc)})
                if n >= self.acfg.verify_attempts:
                    self.verify_failed[key] = str(exc)
                    self._skip.add(seg["segment_id"])
                return
            except DriveFull as exc:
                self._drive_full(str(exc))
                return
            except OSError as exc:
                if not self._drive_error(exc):
                    raise
                return

    def _drive_error(self, exc: OSError) -> bool:
        """Classify an error on the archive drive. True if handled (removed / full)."""
        att = self.att
        if att is not None and not self.io.root_present(Path(att.target.root)):
            self._detached(unexpected=True)
            self._set(ST_ABSENT, "The archive drive was removed without preparing it. No recordings were lost; "
                                 "copying will continue when a drive is inserted. Recording continues.")
            return True
        if is_disk_full(exc):
            self._drive_full(f"{type(exc).__name__}: {exc}")
            return True
        return False

    def _drive_full(self, detail: str) -> None:
        if not self.full:
            self._event("warning", "archive_drive_full", "Archive drive is full",
                        {"drive_id": self.att.drive_id if self.att else None, "detail": detail})
        self.full = True
        self._set(ST_FULL, "The archive drive is full. Replace it with an empty archive drive. Recording continues.")

    def _source_chunks(self, src: Path) -> Iterator[bytes]:
        try:
            fh = open(src, "rb", buffering=0)
        except OSError as exc:
            raise LocalReadError(exc.errno, f"cannot open local recording: {exc}") from None
        with fh:
            while True:
                if self._should_abort():
                    raise Aborted()
                try:
                    b = fh.read(self.chunk)
                except OSError as exc:
                    raise LocalReadError(exc.errno, f"cannot read local recording: {exc}") from None
                if not b:
                    return
                self.read_throttle.consume(len(b))
                yield b

    def _should_abort(self) -> bool:
        if self._stop.is_set():
            return True
        return self._swap.is_set() and self._swap_deadline is not None and self.mono() > self._swap_deadline

    def _read_hash(self, path: Path) -> tuple[str, int, str]:
        h = hashlib.sha256()
        n = 0
        chunks, method = self.io.read_back(path)
        for c in chunks:
            h.update(c)
            n += len(c)
            self.verify_throttle.consume(len(c))
        return h.hexdigest(), n, method

    def _archive_file(self, seg: Any, kind: str) -> None:
        att = self.att
        assert att is not None and self.al is not None
        root = Path(att.target.root)
        rel = layout.rel_path_for(seg, kind)
        dest = root / rel
        if kind == layout.KIND_FRAMEHASH:
            src_rel, expected, size = seg["framehash_rel_path"], seg["framehash_sha256"], None
        else:
            src_rel, expected, size = seg["rel_path"], seg["sha256"], seg["size_bytes"]
        src = self.cfg.spool_path / src_rel
        if size is None:
            try:
                size = src.stat().st_size
            except OSError as exc:
                raise LocalReadError(exc.errno, f"local recording missing: {exc}") from None
        if self.io.exists(dest):
            sha, n, method = self._read_hash(dest)
            if sha == expected and n == size:
                self._verified(seg, kind, rel, dest, sha, n, method, already_present=True)
                return
            aside = self.io.rename_aside(dest, f".unverified-{compact_utc(time.time())}")
            self._event("warning", "archive_unverified_moved", "A file on the archive drive did not match and was "
                        "moved aside (not deleted, not counted)", {"path": rel, "moved_to": aside.name, "found": sha})
        if not self._space_available(int(size)):
            raise DriveFull(f"need {size} + reserve {self.reserve} bytes")
        t0 = self.mono()
        self.io.write_new(dest, self._source_chunks(src), expected_sha256=expected, fsync_every=self.fsync_bytes)
        t1 = self.mono()
        sha, n, method = self._read_hash(dest)
        t2 = self.mono()
        if sha != expected or n != size:
            aside = self.io.rename_aside(dest, f".unverified-{compact_utc(time.time())}")
            raise VerifyMismatch(f"{rel}: read back {sha} ({n} bytes), sealed {expected} ({size} bytes); "
                                 f"moved to {aside.name}")
        self.stats.files += 1
        self.stats.bytes += n
        self.stats.copy_s += t1 - t0
        self.stats.verify_s += t2 - t1
        self.stats.last = {"path": rel, "bytes": n, "copy_s": round(t1 - t0, 3), "verify_s": round(t2 - t1, 3),
                           "copy_mb_s": round(n / 1e6 / max(t1 - t0, 1e-6), 1),
                           "verify_mb_s": round(n / 1e6 / max(t2 - t1, 1e-6), 1)}
        self._verified(seg, kind, rel, dest, sha, n, method, already_present=False)

    def _verified(self, seg: Any, kind: str, rel: str, dest: Path, sha: str, n: int, method: str,
                  already_present: bool) -> None:
        att = self.att
        assert att is not None and self.al is not None
        self.io.seal_read_only(dest)
        verified_utc = self.al.record_verified(segment_id=seg["segment_id"], drive_id=att.drive_id, kind=kind,
                                               path=rel, sha256=sha, size_bytes=n, verify_method=method,
                                               already_present=already_present)
        att.copies_since_index += 1
        att.copies_this_attach += 1
        if kind in layout.MEDIA_KINDS:
            att.footage_since_index += float(seg["duration_s"] or 0.0)
        journal = Path(att.target.root) / layout.JOURNAL
        try:
            if not journal.exists():
                self.io.append_line(journal, idx.JOURNAL_HEADER)
            self.io.append_line(journal, idx.journal_line(verified_utc, seg["segment_id"], kind, sha, n, rel))
        except OSError as exc:   # the index written later covers this copy
            log.warning("could not append to the archive journal: %s", exc)
            if not self._drive_error(exc):
                att.stale_index = True
        self._publish()

    # ================================================================== index
    def _write_index(self, reason: str) -> bool:
        att = self.att
        assert att is not None and self.al is not None and self.ledger is not None
        root = Path(att.target.root)
        copies = self.al.copies_on(att.drive_id)
        media = [c for c in copies if c["kind"] in layout.MEDIA_KINDS]
        gaps: list[Any] = []
        not_on: list[dict[str, Any]] = []
        if media:
            first = min(int(c["seg_rowid"]) for c in media)
            last = max(int(c["seg_rowid"]) for c in media)
            starts = [parse_iso_utc(c["start_utc"]) for c in media if c["start_utc"]]
            ends = [parse_iso_utc(c["end_utc"]) for c in media if c["end_utc"]]
            lo, hi = (min(starts), max(ends)) if starts and ends else (None, None)
            for g in self.ledger.gaps(self.camera_id):
                gs = parse_iso_utc(g["start_utc"]) if g["start_utc"] else None
                ge = parse_iso_utc(g["end_utc"]) if g["end_utc"] else None
                if lo is None or ((gs is None or gs <= hi) and (ge is None or ge >= lo)):
                    gaps.append(g)
            have = {c["segment_id"] for c in media}
            span = [s for s in self.al.segments_in_span(first, last) if s["segment_id"] not in have]
            elsewhere = self.al.other_drives_holding([s["segment_id"] for s in span], att.drive_id)
            labels = {r["drive_id"]: (r["label"] or r["drive_id"]) for r in self.ledger.query(
                "SELECT drive_id, label FROM drives")}
            for s in span:
                others = elsewhere.get(s["segment_id"], [])
                sealed = s["state"] in ("FINALIZED", "INTERRUPTED", "CORRUPT")
                not_on.append({"what": "on_another_archive_drive" if others else
                               ("not_yet_archived" if sealed else "still_being_recorded_or_checked"),
                               "segment_id": s["segment_id"], "segment_state": s["state"],
                               "start_utc": s["start_utc"], "end_utc": s["end_utc"], "duration_s": s["duration_s"],
                               "other_drives": [labels.get(o, o) for o in others]})
        gen = self.al.next_index_generation(att.drive_id)
        generated = iso_utc(time.time())
        drive = dict(att.target.identity, drive_id=att.drive_id)
        files = idx.build_index(generation=gen, generated_utc=generated, camera_id=self.camera_id, tz=self.tz,
                                drive=drive, software={"app_version": __version__}, policy=self.acfg.resume_policy,
                                copies=copies, gaps=gaps, not_on_drive=not_on)
        try:
            # data files first, SHA256SUMS last; each file is complete and correct on its own
            for rel in (layout.INDEX_JSON, layout.INDEX_CSV, layout.GAPS_CSV, layout.SUMS):
                self.io.replace_file(root / rel, files[rel], verify=lambda p: self._read_hash(p)[0])
        except OSError as exc:
            log.warning("archive index write failed: %s", exc)
            att.stale_index = True
            if not self._drive_error(exc):
                self._event("warning", "archive_index_failed", "Archive drive index could not be written",
                            {"error": f"{type(exc).__name__}: {exc}"})
            return False
        self.al.record_index(att.drive_id, gen, generated, reason, len(copies),
                             hashlib.sha256(files[layout.INDEX_JSON]).hexdigest(),
                             hashlib.sha256(files[layout.SUMS]).hexdigest())
        att.footage_since_index = 0.0
        att.copies_since_index = 0
        att.last_index_mono = self.mono()
        att.stale_index = False
        log.info("archive index generation %d written to %s (%s, %d files)", gen, att.drive_id, reason, len(copies))
        return True

    # ================================================================== status
    def _set(self, state: str, message: str) -> None:
        changed = state != self.state
        self.state, self.message = state, message
        self._publish(force=changed)

    def conditions(self) -> tuple[Condition, Any, Any]:
        assert self.al is not None
        risk = self.al.backlog(None)
        queue = self.al.backlog(self.att.drive_id) if self.att is not None else None
        cond = Condition(state=self.state, backlog_s=risk.seconds, threshold_s=self.acfg.backlog_alert_hours * 3600,
                         removed_unexpectedly=self.removed_unexpectedly, after_swap=self.after_swap, full=self.full,
                         unusable=self.state == ST_UNUSABLE,
                         verify_failed=bool(self.verify_failed or self.missing_on_drive),
                         source_unreadable=bool(self.source_failed), software_errors=self.errors)
        return cond, risk, queue

    def alerts(self) -> list[Alert]:
        return evaluate_alerts(self.conditions()[0])

    def _publish(self, force: bool = False) -> None:
        now = self.mono()
        if not force and now - self._last_publish < 1.0:
            return
        self._last_publish = now
        if self.al is None:
            self.status.write(state=self.state, message=self.message, alerts=[])
            return
        cond, risk, queue = self.conditions()
        alerts = evaluate_alerts(cond)
        drive = ("full" if self.full else "present") if self.att is not None or self._swapped_drive else (
            "unusable" if self.state == ST_UNUSABLE else "absent")
        text = backlog_text(risk.seconds)
        st = self.stats
        self.status.write(
            state=self.state, drive=drive, message=f"{self.message} {text}.", camera_id=self.camera_id,
            drive_id=self.att.drive_id if self.att else self._swapped_drive,
            drive_label=self.att.target.label if self.att else None, policy=self.acfg.resume_policy,
            backlog={"text": text, "seconds": round(risk.seconds, 1), "segments": risk.segments, "bytes": risk.bytes,
                     "oldest_start_utc": risk.oldest_start_utc, "sidecars": risk.sidecars,
                     "threshold_hours": self.acfg.backlog_alert_hours},
            to_copy_to_this_drive=None if queue is None else {"segments": queue.segments,
                                                              "seconds": round(queue.seconds, 1),
                                                              "bytes": queue.bytes, "sidecars": queue.sidecars},
            alerts=[a.to_dict() for a in alerts], alert=alerts[0].to_dict() if alerts else None,
            throughput={"files": st.files, "bytes": st.bytes, "copy_s": round(st.copy_s, 3),
                        "verify_s": round(st.verify_s, 3), "last": st.last,
                        "throttle_mb_s": self.acfg.max_rate_mb_s},
            problems={"verify_failed": list(self.verify_failed.values())[:20],
                      "source_unreadable": sorted(self.source_failed)[:20],
                      "missing_on_drive": self.missing_on_drive[:20], "unusable_reason": self.unusable_reason},
            priority=self.priority, cpu_process_s=round(time.process_time(), 2))

    def _event(self, severity: str, code: str, message: str, detail: dict[str, Any] | None) -> None:
        try:
            self.ledger.add_event(severity, "archive", code, message, detail)  # type: ignore[union-attr]
        except Exception:
            log.exception("could not record archive event %s", code)


def request_swap_file(cfg: AppConfig) -> Path:
    """Used by the CLI / GUI in another process: ask the running archive worker to swap."""
    d = cfg.data_path / CONTROL_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / SWAP_REQUEST
    p.write_text(iso_utc(time.time()), "utf-8")
    return p


def request_resume_file(cfg: AppConfig) -> Path:
    d = cfg.data_path / CONTROL_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / RESUME_REQUEST
    p.write_text(iso_utc(time.time()), "utf-8")
    return p
