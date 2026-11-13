"""Continuous archive copy to an attached archive drive (requirement S1; docs/04 §2-3).

Mechanism (design choice, see docs/branches/design-archive-drive.md):

* Recording always lands on the internal storage ("spool"); this worker copies every sealed
  segment (FINALIZED / INTERRUPTED; CORRUPT into ``Quarantine/``) and its per-frame sidecar,
  **oldest first**, to whichever archive drive is attached. If the drive is slow, absent or full,
  the footage simply waits on internal storage ("Not yet archived: N h").
* Archive drives are found by their marker (``SENTINEL_DRIVE.json`` with ``"role": "archive"``) on
  any mounted volume, never by drive letter (:mod:`sentinel.archive.locator`).
* Each file: 8 MiB chunks read from the spool through a byte-rate :class:`Throttle`, written to
  ``<name>.partial`` with an fsync every 64 MiB, fsync, no-replace rename, directory flush (Windows:
  FlushFileBuffers on the directory; if refused, the final name is confirmed by a fresh listing);
  then read back from the drive bypassing the OS cache and compared with the SHA-256 recorded at
  sealing. Only then: journal line appended and flushed, ledger row VERIFIED. A buffered (cache)
  read-back is never accepted. Nothing on the local spool is ever modified or deleted.
* On attach the drive is reconciled FIRST: recorded copies that are missing are restored from their
  ``.partial`` if that still matches the sealed hash (a rename rolled back by an unclean unplug),
  otherwise marked LOST (row kept, segment queued again, persistent alert A-006); only then are
  stray ``.partial`` files deleted.
* ``Sentinel/<camera>/INDEX/`` is rewritten on attach, after each ``index_every_s`` of copied
  footage (default one hour; also after an hour of wall time with new copies) and on swap.
* Swap: :meth:`ArchiveWorker.request_swap` (or ``python -m sentinel.archive swap``) → the file in
  flight is finished and verified if that takes at most ``swap_max_wait_s``, otherwise abandoned
  (its ``.partial`` is discarded, never claimed) → index written and read back → flush → eject via
  the :class:`~sentinel.export.target.SafeRemover` (fallback: operator uses the OS "safely remove")
  → SAFE_TO_REMOVE / REMOVE_MANUALLY. The swapped drive is not written again until it has been
  removed (or the operator resumes); another archive drive inserted meanwhile is used. The swapped
  drive is remembered in ``archive_control/swapped_drive.json`` across a restart of this process, the
  request file is also looked for while a copy is in progress, and a request older than 15 minutes is
  ignored. Operators (the window) ask through ``<status_dir>/requests/`` (untrusted, see
  :meth:`ArchiveWorker._take_operator`).
* Two drives (owner request 2026-11-11, two-bay dock): when the drive in use is full and another
  archive drive with room is inserted, its index is written and copying moves on to the other drive
  by itself ("rollover"); nothing is lost and recording is never paused. Swap then prepares the FULL
  drive for removal while copying continues on the other one. The status lists every archive drive
  inserted (``drives``) and the drive waiting to be taken out (``removal``).
* Return to service (0.3.9, owner request 2026-11-12): after the office has copied a full archive drive
  to its own storage and checked every file, an administrator returns the drive (``python -m
  sentinel.archive return``, the window's "Return to service"). The request reaches this process through
  its control folder only (administrators), and is carried out between two files, never during a copy:
  every recorded copy must still be on the drive with its recorded size (else nothing happens), the
  copies are recorded as TRANSFERRED to the place named (they still count as archived), and only then
  are those files, the index and the playlists deleted from the drive. The drive keeps its marker and
  label and is used again like an empty one.

Runs as its own process in production (``python -m sentinel.archive run``) — see
:mod:`sentinel.archive.__main__` for why — and as a thread in tests (:meth:`start`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
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
from ..export.fileio import M_BUFFERED, PARTIAL, SourceMismatch, is_disk_full, partial_path
from ..export.ledger import ExportLedger
from ..export.target import (ExportTarget, ManualRemovalFallback, SafeRemover, TargetError,
                             protected_volume_conflicts, system_paths)
from ..platform import get_platform
from ..platform.base import InstanceLockError
from . import index as idx
from . import layout
from .io import MIB, Aborted, ArchiveIO, Throttle
from .ledger import ArchiveLedger, UnverifiableCopy, ledger_instance
from .locator import DriveLocator, DriveScan, VolumeScanLocator
from .priority import lower_priority, normal_priority
from .status import (ST_ABSENT, ST_ATTENTION, ST_COPYING, ST_FULL, ST_REMOVE_MANUALLY, ST_SAFE, ST_STARTING,
                     ST_STOPPED, ST_SWAP_PENDING, ST_UNUSABLE, ST_UP_TO_DATE, STATUS_FILE, ArchiveStatusWriter,
                     Condition, backlog_text, evaluate_alerts)

log = logging.getLogger(__name__)

CONTROL_DIR = "archive_control"
SWAP_REQUEST = "swap.request"
RESUME_REQUEST = "resume.request"
RETURN_REQUEST = "return.request"   # JSON from ``archive return`` (control folder only: administrators)
RETURN_RESULT = "return.result.json"
RETURN_REQUEST_MAX_BYTES = 8192
SWAPPED_STATE = "swapped_drive.json"   # the drive made safe to remove, kept across a restart of the worker
# Review 2026-11-12: drives taken out with Swap are not written again until they are returned to service
# (the office copy is of what the drive held then) or the operator presses Cancel swap.
AWAITING_STATE = "awaiting_return.json"
REQUEST_MAX_AGE_S = 900.0        # a swap/resume request older than this is stale and not acted on
CONTROL_POLL_S = 1.0             # how often a copy in progress looks for a swap request
HEARTBEAT_S = 10.0               # status heartbeat while one file is copied or read back (a one-hour file
                                 # takes minutes; the window calls copying "not running" after 60 s)
OPERATOR_REQUESTS = "requests"   # sub-folder of [storage] status_dir writable by operators (the window)
OPERATOR_REQUEST_MAX_BYTES = 1024
UNCERTAIN_MISSES = 10            # scans to wait for the attached drive while the volume list is unreadable
PROBE = ".sentinel_archive_probe" + PARTIAL
SOURCE_RETRY_S = 3600.0          # retry a local recording that could not be read after this long
REVIEW_LAYOUT = ("README_FIRST.txt", "Recordings", "Manifests", "Originals")

# Review 2026-11-12: the deployed copier has no software eject (ManualRemovalFallback), so the screen never says
# SAFE TO REMOVE; it says which drive to take out and how (Safely Remove Hardware, or "ready to be taken out").
MSG_FULL = ("Do not unplug the archive drive yet. It is full: press Swap Archive Drive and wait until the screen "
            "says which drive to take out and how, then take out only that drive and insert an empty archive drive.")
FULL_RECHECK_S = 600.0           # a full drive left in the dock is looked at again after this long
MSG_REMOVED = ("The archive drive was removed without preparing it. No recordings were lost; copying will "
               "continue when a drive is inserted.")


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
    next_index_retry: float = 0.0


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
        self.locator: DriveLocator = locator or VolumeScanLocator(
            [self.acfg.drive_path] if self.acfg.drive_path else [], require_role=self.acfg.require_drive_marker,
            roots=None if self.acfg.scan_volumes else (lambda: []))
        self.io = io or ArchiveIO(read_chunk=self.acfg.chunk_mib * MIB)
        self.remover: SafeRemover = remover or ManualRemovalFallback()
        self.status = ArchiveStatusWriter(status_path or (cfg.data_path / STATUS_FILE))
        self.allow_same_volume = allow_same_volume
        self.mono = mono
        self.lower_own_priority = lower_own_priority
        self.tz = ZoneInfo(cfg.recording.display_timezone)
        self.camera_id = cfg.camera.id
        self.index_names = layout.index_files(self.camera_id)
        self.control_dir = cfg.data_path / CONTROL_DIR
        # Operators' request folder (window): only when a separate status folder exists, else the
        # requests go to control_dir and are read once, from there.
        self.operator_dir = operator_request_dir(cfg) if cfg.storage.status_dir else None
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
        self.source_failed: dict[str, float] = {}   # segment_id -> mono time of failure (alert A-008)
        self.errors = 0                             # consecutive unexpected errors (alert A-007 at >= 3)
        self._verify_attempts: dict[str, int] = {}
        self._skip: set[str] = set()                # segment ids skipped on the current drive
        # control
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._swap = threading.Event()
        self._swap_deadline: float | None = None
        self._swapped_drive: str | None = None
        self._swapped_label: str | None = None
        self._swapped_absent_polls = 0
        self._present: dict[str, ExportTarget] = {}  # archive drives inserted (last scan), the swapped one excluded
        self._full_drives: dict[str, float] = {}    # drive_id -> mono time it was found full
        self._refused: dict[str, float] = {}        # drive_id -> mono time _attach refused it (review 2026-11-12)
        self._swap_result: tuple[bool, str] | None = None
        self._last_swapped: str | None = None       # drive last prepared for removal (session gone_how)
        self._att_missed = 0                        # consecutive scans that did not see the attached drive
        self._last_control_poll = float("-inf")
        self._last_heartbeat = float("-inf")
        self._thread: threading.Thread | None = None
        self._last_publish = 0.0
        self._instance: str | None = None
        self._last_scan: tuple[DriveScan | None, bool] = (None, False)
        self._awaiting: dict[str, str | None] = {}           # drive_id -> label, swapped and not yet returned
        self._awaiting_since: dict[str, float] = {}          # drive_id -> when it was swapped (epoch)
        self._awaiting_present: dict[str, ExportTarget] = {}
        self._release_present = False                        # Cancel swap: release awaiting drives inserted now
        self._return_checked: set[str] = set()               # awaiting drives looked at for an interrupted return
        self._leftover_drives: set[str] = set()              # drives with transferred files not yet deleted
        self._load_swapped()
        self._load_awaiting()

    # ================================================================== public control
    def request_swap(self) -> None:
        """Operator pressed "Swap Archive Drive": pause at the next segment boundary, then make the
        drive safe to remove. Recording is not affected."""
        if self._swapped_drive is not None:
            # Review 2026-11-12: a second press while a drive waits to be taken out armed a swap that then
            # prepared the drive in use (or the next one inserted) and made the first drive writable again.
            log.info("swap request ignored: %s is still waiting to be taken out",
                     self._swapped_label or self._swapped_drive)
            self._last_publish = float("-inf")
            self._wake.set()
            return
        if self.att is None and self.state == ST_UNUSABLE:
            # Review 2026-11-12: pressed while the inserted drive is refused, the swap stayed armed and prepared the
            # next good drive (put on the awaiting-return list, never written). A refused drive is not written:
            # there is nothing to prepare, and it can be taken out.
            self._drop_swap("the inserted drive cannot be used as an archive drive")
            return
        if not self._swap.is_set():
            self._swap_deadline = self.mono() + self.acfg.swap_max_wait_s
            self._swap.set()
        self._wake.set()

    def resume(self) -> None:
        """Cancel a pending swap, or continue on a drive that was made safe to remove but not removed, or on a
        drive back from the office that is inserted now. Review 2026-11-12: drives that are not inserted (at the
        office) stay on the awaiting-return list; Cancel swap used to release every one of them."""
        self._swap.clear()
        self._swap_deadline = None
        release = set(self._awaiting_present)
        if self._swapped_drive is not None:
            release.add(self._swapped_drive)
        for k in sorted(release):
            self._set_awaiting(k, remove=True)
        self._release_present = True        # and those the next scan finds inserted (request read before it)
        self._set_swapped(None)
        self._last_swapped = None
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
            while not self._stop.is_set():
                try:
                    if self.al is None:
                        self._open()
                        self._publish(force=True)
                    outcome = self.step()
                    self.errors = 0
                except Exception as exc:  # never die: report (itself guarded) and retry
                    self.errors += 1
                    outcome = "error"
                    log.exception("archive step failed (%d consecutive)", self.errors)
                    self._report_error(exc)
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
                try:
                    self.ledger.close()
                except Exception:
                    log.exception("could not close the ledger")
                self.ledger = None
                self.al = None
            lock.close()

    def _report_error(self, exc: BaseException) -> None:
        """Error path of the loop. Every part is guarded: a failure here (ledger locked, status file
        unwritable, ...) must never end the worker thread/process."""
        try:
            self._event("error", "archive_error", "Archive copy error", {"error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            log.exception("could not record the archive error")
        try:
            self._set(ST_ATTENTION, "Archive copying had a problem and will retry automatically.")
        except Exception:
            log.exception("could not publish the archive error")
            try:
                self.state = ST_ATTENTION
                self.status.write(state=ST_ATTENTION, message="Archive copying had a problem and will retry "
                                  "automatically.", alerts=[], errors=self.errors)
            except Exception:
                log.exception("could not write a minimal archive status")

    def _open(self) -> None:
        if self.ledger is None:
            self.ledger = Ledger(self.cfg.data_path / "sentinel.db")
        if self.al is None:
            self.al = ArchiveLedger(self.ledger, self.camera_id, self.acfg.resume_policy)
            self._instance = ledger_instance(self.ledger)

    # ------------------------------------------------------------------ drive selection
    def _select(self, scan: DriveScan) -> tuple[ExportTarget | None, bool]:
        """(drive to use, is the swapped drive still inserted). Sticky: the attached drive stays in use
        while present; the drive that was made safe to remove is never chosen."""
        by_key: dict[str, ExportTarget] = {}
        for t in scan.candidates:
            try:
                by_key[t.drive_key] = t
            except TargetError:
                continue
        swapped_present = False
        if self._swapped_drive is not None:
            if self._swapped_drive in by_key:
                swapped_present = True
                self._swapped_absent_polls = 0
            elif scan.unusable:
                swapped_present = True     # a drive is present but unreadable: it may be the swapped one
            else:
                self._swapped_absent_polls += 1
                if self._swapped_absent_polls >= 2:     # gone for two scans in a row: really removed
                    self._set_swapped(None)
                else:
                    swapped_present = True
        if self._release_present:
            self._release_present = False
            for k in sorted(k for k in by_key if k in self._awaiting):
                self._set_awaiting(k, remove=True)
        self._awaiting_present = {k: t for k, t in by_key.items() if k in self._awaiting and k != self._swapped_drive}
        usable = {k: t for k, t in by_key.items() if k != self._swapped_drive and k not in self._awaiting}
        self._present = usable
        now = self.mono()
        for k in list(self._full_drives):
            # forget a full drive once it has been taken out; one left in the dock is measured again now and
            # then (space may have been freed on it) and stays "full" while it has no room
            if k not in by_key:
                del self._full_drives[k]
            elif (now - self._full_drives[k] >= FULL_RECHECK_S and (self.att is None or k != self.att.drive_id)):
                if self._has_room(by_key[k]):
                    del self._full_drives[k]
                else:
                    self._full_drives[k] = now
        if self.att is not None and self.att.drive_id in usable and self.att.drive_id not in self._full_drives:
            return usable[self.att.drive_id], swapped_present
        if not usable:
            return None, swapped_present
        for k in list(self._refused):      # tried again once it was re-inserted, or after a while
            if k not in by_key or now - self._refused[k] >= FULL_RECHECK_S:
                del self._refused[k]
        room = sorted(k for k in usable if k not in self._full_drives)
        if len(room) > 1:
            # First review run, 2026-11-12: a drive the copier refuses (review layout, write test failed, ...)
            # was chosen again at every scan and the good drive next to it was never used.
            room = [k for k in room if k not in self._refused] or room
        if room:
            if self.att is None and len(usable) > 1:
                log.info("%d archive drives inserted; using %s", len(usable), room[0])
            return usable[room[0]], swapped_present
        # every inserted drive is full: stay on the one in use (status FULL)
        if self.att is not None and self.att.drive_id in usable:
            return usable[self.att.drive_id], swapped_present
        return usable[sorted(usable)[0]], swapped_present

    def step(self) -> str:
        """One unit of work: look at the drive(s), then copy+verify at most one segment. Returns
        'absent' | 'unusable' | 'waiting_for_removal' | 'swapped' | 'full' | 'idle' | 'copied' | 'rescan'
        ('rescan': the attached drive was not seen by this scan; it counts as removed only when the next
        scan does not see it either).
        Afterwards the drive presence sessions are updated (used by the retention rule)."""
        result = self._step()
        try:
            self._sync_sessions()
        except Exception:
            log.exception("could not update archive drive presence")
        return result

    def _sync_sessions(self) -> None:
        scan, swapped_present = self._last_scan
        if scan is None or self.al is None:
            return
        present = set()
        for t in scan.candidates:
            try:
                present.add(t.drive_key)
            except TargetError:
                continue
        if swapped_present and self._swapped_drive:
            present.add(self._swapped_drive)
        if self.att is not None:            # attached drive missed by one scan: not yet proven removed
            present.add(self.att.drive_id)
        # (review 2026-11-10) the swapped drive is cleared from _swapped_drive on the scan that proves its
        # removal, before this runs: pass the drive last prepared for removal so that a proper swap is
        # recorded as 'swapped', not 'removed'.
        self.al.sync_sessions(present, keep_open=bool(scan.unusable) or bool(scan.uncertain),
                              swapped=self._swapped_drive or self._last_swapped)

    def _step(self) -> str:
        self._open()
        self._poll_control()
        scan = self.locator.scan()
        self._poll_return(scan)           # first: a drive returned to service is used from this scan on
        self._finish_interrupted_returns(scan)
        target, swapped_present = self._select(scan)
        self._last_scan = (scan, swapped_present)
        if self.att is not None and self.att.drive_id not in self._present:
            # (review 2026-11-10) one scan that does not see the attached drive (drive not ready,
            # volume list or marker momentarily unreadable) is not proof that it was removed: no
            # A-003, presence session kept open. Gone for two scans in a row: removed.
            # While the volume list itself cannot be read, wait longer before concluding.
            # Review 2026-11-12: the same when another archive drive is inserted (it used to be attached at once).
            self._att_missed += 1
            if self._att_missed < (UNCERTAIN_MISSES if scan.uncertain else 2):
                log.info("attached archive drive not seen by this scan (%d)%s", self._att_missed,
                         f"; {scan.uncertain}" if scan.uncertain else "")
                return "rescan"
            self._detached(unexpected=True)
        if target is None:
            if swapped_present:
                self._waiting_for_removal()
                return "waiting_for_removal"
            res = self._no_drive(scan)
            if res == "absent" and self._awaiting_present:
                self._set(ST_ABSENT, self._awaiting_text() or "")
                self._publish(force=True)
            return res
        key = target.drive_key
        self._att_missed = 0
        if self.att is None or self.att.drive_id != key:
            if self.att is not None:
                if self.att.drive_id in self._present:   # still inserted, but full: move on to the next drive
                    self._roll_over(target)
                else:
                    self._detached(unexpected=True)
            if not self._attach(target):
                if self._swap.is_set():
                    # Review 2026-11-12: see request_swap. Nothing the request was about can be prepared.
                    self._drop_swap("the inserted drive could not be used as an archive drive")
                return "unusable"
        elif target.label and target.label != self.att.target.label:
            # Renamed while attached (Drives › Rename…, 0.3.11): same drive, new name from now on.
            log.info("archive drive renamed %s -> %s", self.att.target.label, target.label)
            self.att.target = target
            ExportLedger(self.ledger).upsert_drive(target)  # type: ignore[arg-type]
            self._publish(force=True)
        assert self.att is not None and self.al is not None
        if self._swap.is_set():
            return self._do_swap()
        if self.att.stale_index and self.mono() >= self.att.next_index_retry:
            self._write_index("retry")
            if self.att is None:
                return "absent"
        if self.full and not self._space_available():
            self._full_drives.setdefault(self.att.drive_id, self.mono())
            self._set(ST_FULL, MSG_FULL)
            return "full"
        self.full = False
        self._full_drives.pop(self.att.drive_id, None)
        self._expire_source_failures()
        pending = self.al.pending_segments(self.att.drive_id, limit=1,
                                           exclude=frozenset(self._skip | set(self.source_failed)))
        if not pending:
            if self.att.copies_since_index and self.mono() - self.att.last_index_mono >= self.acfg.index_every_s:
                self._write_index("hourly")
            if self._skip or self.source_failed:
                self._set(ST_ATTENTION, "Some recordings could not be copied to the archive drive.")
            else:
                self._set(ST_UP_TO_DATE, "Archive drive connected. All recordings are copied.")
            return "idle"
        self._set(ST_COPYING, "Copying recordings to the archive drive. Do not unplug it.")
        self._archive_segment(pending[0])
        if self.att is not None and (self.att.footage_since_index >= self.acfg.index_every_s
                                     or (self.att.copies_since_index
                                         and self.mono() - self.att.last_index_mono >= self.acfg.index_every_s)):
            self._write_index("hourly")
        return "copied"

    # ================================================================== drive presence
    def _no_drive(self, scan: DriveScan) -> str:
        if self._swap.is_set():               # nothing left to prepare
            self._clear_swap()
        if scan.unusable:
            self.unusable_reason = "; ".join(r for _root, r in scan.unusable)[:500]
            self._set(ST_UNUSABLE, "The inserted drive cannot be used as an archive drive.")
            return "unusable"
        self.unusable_reason = None
        if self.after_swap:
            self._set(ST_ABSENT, "Insert the next archive drive.")
        elif self.removed_unexpectedly:
            self._set(ST_ABSENT, MSG_REMOVED)
        else:
            self._set(ST_ABSENT, "No archive drive is connected. Recordings stay on this computer until one is inserted.")
        return "absent"

    def _detached(self, unexpected: bool) -> None:
        att = self.att
        self.att = None
        self._att_missed = 0
        self.full = False
        self._skip.clear()
        if att is None:
            return
        if unexpected:
            if self._swap.is_set():
                # Review 2026-11-12: the drive the swap was pressed for is gone; a pending swap must not prepare
                # whichever drive is attached next.
                self._drop_swap("the archive drive in use was removed without preparing it")
            self.removed_unexpectedly = True
            self._event("warning", "archive_drive_removed", "Archive drive removed without preparing it",
                        {"drive_id": att.drive_id, "copies_this_attach": att.copies_this_attach})
            log.warning("archive drive %s removed without preparing", att.drive_id)

    def _refuse(self, target: ExportTarget, reason: str) -> bool:
        try:
            self._refused[target.drive_key] = self.mono()
        except TargetError:
            pass
        self.unusable_reason = reason
        self._event("warning", "archive_drive_unusable", "Archive drive refused",
                    {"reason": reason, "identity": target.identity})
        self._set(ST_UNUSABLE, "The inserted drive cannot be used as an archive drive.")
        return False

    def _attach(self, target: ExportTarget) -> bool:
        assert self.al is not None
        root = Path(target.root)
        if not self.io.root_present(root):
            return False
        if not self.allow_same_volume:
            # Same fail-closed rule as the review export: never archive onto the volume holding the
            # recordings, the ledger, the logs or the operating system.
            protected = {"recording storage": self.cfg.spool_path, "recorder database": self.cfg.data_path,
                         "recorder logs": self.cfg.log_path, **system_paths()}
            try:
                clash = protected_volume_conflicts(root, protected)
            except TargetError as exc:
                clash = [f"(cannot confirm it is a separate drive: {exc})"]
            if clash:
                return self._refuse(target, "the archive location is on the same volume as the " + ", ".join(clash))
        review = [n for n in REVIEW_LAYOUT if (root / n).exists()]
        if review:
            return self._refuse(target, f"the drive holds a review-drive layout ({', '.join(review)})")
        if self.al.review_batches_on(target.drive_key):
            return self._refuse(target, "review exports to this drive are recorded in the ledger")
        full = False
        try:
            self.io.makedirs(root / layout.index_dir(self.camera_id))
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
                return self._refuse(target, f"write test failed: {type(exc).__name__}: {exc}")
        with normal_priority():
            drive_id = ExportLedger(self.ledger).upsert_drive(target)  # type: ignore[arg-type]
        self.att = Attached(target=target, drive_id=drive_id, last_index_mono=self.mono())
        if self._last_swapped == drive_id:      # in use again: a later removal is not that swap
            self._last_swapped = None
        self.removed_unexpectedly = self.after_swap = False
        self.unusable_reason = None
        self.full = full
        if full:
            self._full_drives.setdefault(drive_id, self.mono())
        self._skip.clear()
        self._verify_attempts.clear()
        try:
            restored, lost, removed = self._reconcile(root, drive_id)
            q = self.al.backlog(drive_id)
        except BaseException as exc:
            # Review 2026-11-12: the drive stayed attached without its reconcile, which was never run again for
            # this insertion (missing copies kept counting as archived). Not attached: the next step tries again.
            handled = isinstance(exc, OSError) and self._drive_error(exc)     # removed (detached) or full
            if self.att is not None and self.att.drive_id == drive_id:
                self.att = None
            if handled:
                return False
            raise
        self._clean_leftovers(root, drive_id)
        self._event("info", "archive_drive_attached", "Archive drive connected",
                    {"drive_id": drive_id, "label": target.label, "restored": restored, "lost": lost,
                     "discarded_partials": removed, "to_copy_segments": q.segments,
                     "to_copy_seconds": round(q.seconds, 1), "full": full})
        log.info("archive drive %s attached (%d restored, %d lost, %d partial(s) discarded, %d to copy)", drive_id,
                 restored, lost, removed, q.segments)
        if not (root / layout.README).exists():
            try:
                self.io.write_new(root / layout.README, [idx.README_TEXT.encode("utf-8")])
            except FileExistsError:
                pass
            except OSError as exc:
                if not self._drive_error(exc):
                    raise
                return self.att is not None
        try:
            self._write_index("attach")
        except Exception:
            # (review 2026-11-12) an unexpected error writing the attach index is retried like a failed write,
            # not forgotten until the next attach
            if self.att is not None:
                self.att.stale_index = True
                self.att.next_index_retry = self.mono() + 60.0
            raise
        return self.att is not None

    def _reconcile(self, root: Path, drive_id: str) -> tuple[int, int, int]:
        """FIRST on attach, before any temporary file is deleted: every recorded copy must be on the
        drive with its recorded size. A missing one whose ``.partial`` still matches the sealed hash is
        restored (its rename was rolled back by an unclean removal); otherwise it is marked LOST.
        Then the remaining ``.partial`` files (never claimed) are deleted. Returns (restored, lost, removed)."""
        assert self.al is not None
        partials = {p for p in self.io.stray_partials(root / layout.ROOT)}
        restored = lost = 0
        for c in self.al.copies_on(drive_id):
            p = root / c["path"]
            try:
                size = p.stat().st_size
            except FileNotFoundError:
                size = None
            except OSError as exc:
                # Review 2026-11-12: one unreadable entry (damaged folder entry after an unclean unplug) aborted the
                # whole reconcile. Unless the drive itself has gone, the copy cannot be confirmed: LOST (re-copied).
                if not self.io.root_present(root):
                    raise
                self.al.mark_lost(c["copy_id"], f"cannot be read on the drive: {type(exc).__name__}: {exc}"[:500])
                lost += 1
                continue
            if size == c["size_bytes"]:
                continue
            if size is not None:
                self.al.mark_lost(c["copy_id"], f"size on the drive is {size}, recorded {c['size_bytes']}")
                lost += 1
                continue
            part = partial_path(p)
            if part in partials and self._restore_from_partial(root, part, p, c):
                partials.discard(part)
                restored += 1
                self._event("warning", "archive_copy_restored", "A verified copy whose file name was lost was "
                            "restored from its temporary file after checking it again", {"path": c["path"]})
                continue
            self.al.mark_lost(c["copy_id"], "file missing from the drive")
            lost += 1
        removed = 0
        for p in sorted(partials):
            try:
                self.io.remove_partial_file(p)
                removed += 1
            except OSError as exc:
                log.warning("could not discard %s: %s", p, exc)
        return restored, lost, removed

    def _restore_from_partial(self, root: Path, part: Path, dest: Path, c: Any) -> bool:
        """Put back a verified copy from its ``.partial`` if that still matches the sealed hash. A ``.partial`` that
        cannot be read or renamed (bad sector, refused rename) is not restorable: the copy is then marked LOST and
        copied again, instead of the error stopping the reconcile (review 2026-11-12). A gone drive still raises."""
        try:
            sha, n, method = self._read_hash(part)
            if sha != c["sha256"] or n != c["size_bytes"] or method == M_BUFFERED:
                return False
            self.io.restore_partial(part, dest)
        except OSError as exc:
            if not self.io.root_present(root):
                raise
            log.warning("could not restore %s from its temporary file: %s", dest, exc)
            return False
        try:
            self.io.seal_read_only(dest)
        except OSError as exc:
            if not self.io.root_present(root):
                raise
            log.warning("could not seal the restored copy %s read-only: %s", dest, exc)
        return True

    def _space_available(self, need: int = 0) -> bool:
        assert self.att is not None and self.al is not None
        try:
            free = self.io.free_bytes(Path(self.att.target.root))
        except OSError:
            return False
        if need == 0:
            nxt = self.al.pending_segments(self.att.drive_id, limit=1)
            need = int(nxt[0]["size_bytes"] or 0) if nxt else 0
        return free >= need + self.reserve

    # ================================================================== swap
    def _poll_control(self) -> None:
        for name, action in ((SWAP_REQUEST, self.request_swap), (RESUME_REQUEST, self.resume)):
            if self._take_request(name):
                action()

    def _take_request(self, name: str) -> bool:
        """Consume a swap / resume request: from the service's own control folder (technician CLI) and,
        when a separate status folder is configured, from the operators' request folder (the window)."""
        got = self._take_trusted(name)
        if self.operator_dir is not None:
            got = self._take_operator(name) or got
        return got

    def _take_operator(self, name: str) -> bool:
        """A request in the operators' folder (writable by standard users) is untrusted input. It is acted
        on only if it is a regular file (not a link, junction or folder), at most OPERATOR_REQUEST_MAX_BYTES,
        and holds an ISO UTC time no older than REQUEST_MAX_AGE_S (and not in the future by more than
        that). It is deleted once read, whatever it held. It can only ever ask for a swap or a resume."""
        assert self.operator_dir is not None
        p = self.operator_dir / name
        kind = name.split(".")[0]
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return False
        except OSError as exc:
            log.warning("operator %s request unreadable: %s", kind, type(exc).__name__)
            return False
        reparse = bool(getattr(st, "st_file_attributes", 0) & 0x400)     # FILE_ATTRIBUTE_REPARSE_POINT
        why = None
        data = b""
        if stat.S_ISLNK(st.st_mode) or reparse or not stat.S_ISREG(st.st_mode):
            why = "not a regular file"
        elif st.st_size > OPERATOR_REQUEST_MAX_BYTES:
            why = "too large"
        else:
            try:
                with open(p, "rb") as fh:
                    data = fh.read(OPERATOR_REQUEST_MAX_BYTES + 1)
            except OSError as exc:
                why = f"unreadable ({type(exc).__name__})"
        if not stat.S_ISDIR(st.st_mode):   # a folder / junction is left alone (ignored), anything else deleted
            try:
                os.unlink(p)               # removes a link itself, never what it points to
            except OSError:
                if why is None:
                    return False           # could not consume it: do not act on it twice
        if why is None:
            try:
                written = parse_iso_utc(data.decode("ascii").strip())
                age = time.time() - written
                if not -REQUEST_MAX_AGE_S <= age <= REQUEST_MAX_AGE_S:
                    why = f"not recent ({round(age)} s old)"
            except (UnicodeDecodeError, ValueError):
                why = "content is not a time"
        if why is not None:
            self._event("warning", f"archive_{kind}_request_ignored",
                        f"An archive {kind} request in the operator folder was ignored", {"why": why,
                                                                                         "source": "operator_folder"})
            log.warning("ignored an operator %s request: %s", kind, why)
            return False
        self._event("audit", f"archive_{kind}_requested", f"Archive {kind} requested from the window",
                    {"source": "operator_folder"})
        log.info("archive %s requested from the operator folder", kind)
        return True

    def _take_trusted(self, name: str) -> bool:
        """Consume a request file in the service's control folder. A request older than REQUEST_MAX_AGE_S
        (for example pressed while the archive process was not running) is removed and NOT acted on, so a
        drive inserted hours later is never swapped by it (review 2026-11-10)."""
        p = self.control_dir / name
        if not p.exists():
            return False
        written = None
        try:
            written = parse_iso_utc(p.read_text("utf-8").strip())
        except (OSError, ValueError):
            try:
                written = p.stat().st_mtime
            except OSError:
                pass
        try:
            p.unlink()
        except OSError:
            return False
        kind = name.split(".")[0]
        age = time.time() - written if written is not None else 0.0
        if age > REQUEST_MAX_AGE_S:
            self._event("warning", f"archive_{kind}_request_expired", f"An old archive {kind} request was ignored",
                        {"age_s": round(age, 1)})
            log.warning("ignored a %s request written %.0f s ago", kind, age)
            return False
        self._event("audit", f"archive_{kind}_requested", f"Archive {kind} requested", None)
        return True

    # ------------------------------------------------------------------ return to service (0.3.9)
    def _poll_return(self, scan: DriveScan) -> None:
        p = self.control_dir / RETURN_REQUEST
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("return request unreadable: %s", type(exc).__name__)
            return
        req: dict[str, Any] = {}
        why = None
        try:
            if not stat.S_ISREG(st.st_mode) or st.st_size > RETURN_REQUEST_MAX_BYTES:
                why = "not a small regular file"
            else:
                doc = json.loads(p.read_text("utf-8"))
                req = doc if isinstance(doc, dict) else {}
                age = time.time() - parse_iso_utc(str(req.get("written_utc")))
                if not -REQUEST_MAX_AGE_S <= age <= REQUEST_MAX_AGE_S:
                    why = f"written {age:.0f} s ago"
        except (OSError, ValueError, TypeError) as exc:
            why = f"unreadable ({type(exc).__name__})"
        try:
            p.unlink()
        except OSError:
            log.exception("could not remove the return request")
            return
        if why:
            self._event("warning", "archive_return_request_ignored", "A request to return an archive drive to "
                        "service was ignored", {"why": why})
            ok, msg, detail = False, "The request was not carried out (it was too old or unreadable). Try again.", {}
        else:
            try:
                ok, msg, detail = self._return_drive(req, scan)
            except OSError as exc:
                log.exception("return to service failed")
                ok, msg, detail = (False, "The drive could not be returned to service because of a drive error. "
                                   "Contact technical support.", {"error": f"{type(exc).__name__}: {exc}"})
        out = {"request_id": req.get("request_id"), "ok": ok, "message": msg, "finished_utc": iso_utc(time.time()),
               **detail}
        try:
            tmp = self.control_dir / (RETURN_RESULT + PARTIAL)
            tmp.write_text(json.dumps(out, indent=2), "utf-8")
            os.replace(tmp, self.control_dir / RETURN_RESULT)
        except OSError:
            log.exception("could not write the return result")
        self._last_publish = float("-inf")

    def _return_drive(self, req: dict[str, Any], scan: DriveScan) -> tuple[bool, str, dict[str, Any]]:
        """Record the drive's copies as moved to the office and clear them from the drive (see the module
        docstring). Returns (done, sentence for the operator, detail)."""
        assert self.al is not None
        fields = {k: str(req.get(k) or "").strip()[:200] for k in ("drive_uuid", "admin_id", "destination",
                                                                     "copied_by", "copied_on")}
        if not all(fields.values()) or req.get("checked") is not True:
            return False, "The request is incomplete. Nothing was deleted.", {}
        target = next((t for t in scan.candidates if t.identity.get("drive_uuid") == fields["drive_uuid"]), None)
        if target is None:
            return False, "That archive drive is not connected. Nothing was deleted.", {}
        drive_id, root, label = target.drive_key, Path(target.root), target.label or "the archive drive"
        copies = self.al.copies_on(drive_id)
        # Review 2026-11-12: only what the drive held when it was taken out with Swap went to the office.
        # A drive in use, one not swapped since its last return, or one written to after its swap would
        # otherwise have copies the office never had recorded as transferred and deleted.
        swap = self.al.last_swap_since_return(drive_id)
        if self.att is not None and self.att.drive_id == drive_id:
            return False, (f"{label} is the archive drive Sentinel is copying to now, so it has not been to the "
                           "office. Nothing was deleted. Select the drive that came back from the office."), {}
        if drive_id == self._swapped_drive:
            # Review 2026-11-12 (first run): prepared for removal but never taken out, so not at the office.
            return False, (f"{label} was prepared for removal but has not been taken out since, so the office "
                           "cannot have copied it. Nothing was deleted. Take it out, have the office copy it, then "
                           "plug it in and return it to service."), {}
        if swap is None and drive_id in self._awaiting and not copies:
            done = self.al.return_since_swap(drive_id)
            if done is not None:
                # Review 2026-11-12: an earlier Return recorded the transfer and was interrupted (power cut, process
                # stopped) before the drive was cleared. Finish it: only files recorded as TRANSFERRED are deleted.
                deleted, failed = self._finish_return(drive_id, target, done)
                msg = (f"{label} had already been returned to service ({done.get('copies', 0)} file(s) recorded as "
                       f"moved to {done.get('destination') or 'the office'}); that return was interrupted and has now "
                       f"been finished: {deleted} file(s) deleted from the drive.")
                return True, msg + self._not_deleted_text(failed), {"files": 0, "bytes": 0, "resumed": True,
                                                                    "not_deleted": len(failed)}
        if swap is None:
            return False, (f"{label} was not taken out with Swap archive drive since it was set up or last returned "
                           "to service, so the office copy cannot be of it. Nothing was deleted. Select the drive "
                           "that came back from the office."), {}
        last = swap["last_copy_id"]
        newer = [c for c in copies if (int(c["copy_id"]) > last if last is not None
                                       else str(c["verified_utc"]) > str(swap["ts_utc"]))]
        if newer:
            self._event("warning", "archive_return_refused", "An archive drive was not returned to service: "
                        "recordings were copied to it after it was taken out for the office",
                        {"drive_id": drive_id, "copied_after_swap": len(newer)})
            return False, (f"{len(newer)} recording(s) were copied to {label} after it was taken out with Swap "
                           "archive drive, so the office copy does not have them. Nothing was deleted. Press Swap "
                           f"archive drive, take {label} out when the page says so, and have the office copy it "
                           "again (into a new folder)."), {"copied_after_swap": len(newer)}
        bad = []
        for c in copies:
            try:
                size = (root / c["path"]).stat().st_size
            except FileNotFoundError:
                size = None
            if size != c["size_bytes"]:
                bad.append(c["path"])
        if bad:
            self._event("warning", "archive_return_refused", "An archive drive was not returned to service: "
                        "recordings recorded on it are missing or changed",
                        {"drive_id": drive_id, "missing_or_changed": len(bad), "first": bad[:5]})
            return False, (f"{len(bad)} recording(s) recorded on {label} are missing or changed, so it was not "
                           "returned to service. Nothing was deleted. Contact technical support."), {"missing": len(bad)}
        total = sum(int(c["size_bytes"]) for c in copies)
        detail = {**fields, "checked": True, "label": target.label, "request_id": req.get("request_id"),
                  "files": len(copies), "bytes": total}
        changed = self.al.record_transfer(drive_id, [int(c["copy_id"]) for c in copies], detail) if copies else 0
        deleted, failed = self._finish_return(drive_id, target, None)
        gb = total / 1e9
        msg = (f"{label} was returned to service: {changed} file(s) ({gb:.1f} GB) recorded as moved to "
               f"{fields['destination']} and deleted from the drive.")
        return True, msg + self._not_deleted_text(failed), {"files": changed, "bytes": total,
                                                           "not_deleted": len(failed)}

    @staticmethod
    def _not_deleted_text(failed: list[str]) -> str:
        if not failed:
            return ""
        return (f" {len(failed)} file(s) could not be deleted now (for example a video from the drive is open). "
                "Close any video from the drive: Sentinel deletes them by itself when it next uses the drive.")

    def _finish_return(self, drive_id: str, target: ExportTarget, resumed: dict[str, Any] | None
                       ) -> tuple[int, list[str]]:
        """After the transfer is recorded: delete every copy recorded as TRANSFERRED that is still on the drive
        (this return's and any left by an earlier one), the index and the playlists; then put the drive back in
        service. Also finishes a return that was interrupted after its transfer was recorded (``resumed``: that
        return's event detail). Safe to repeat: a VERIFIED copy is never deleted (review 2026-11-12)."""
        assert self.al is not None
        root = Path(target.root)
        deleted, failed = self._clear_drive(root, self._leftover_paths(drive_id, root))
        if failed:
            self._leftover_drives.add(drive_id)
        else:
            self._leftover_drives.discard(drive_id)
        self._full_drives.pop(drive_id, None)
        since = self._awaiting_since.get(drive_id)
        self._set_awaiting(drive_id, remove=True)       # in service again: copied to from the next scan
        # Review 2026-11-12 (first run): with a one-bay dock the drive in use is swapped out only to make room for
        # the drive coming back from the office (docs/10). Drives swapped after this one's swap and before its
        # return are such drives: use them again. (One that went to the office as well is safe: copies made to it
        # after its swap stop its own return until it is swapped and copied again.)
        if since is not None:
            for other in [k for k, t in self._awaiting_since.items() if t > since]:
                self._set_awaiting(other, remove=True)
        if self.att is not None and self.att.drive_id == drive_id:
            self.full = False
            self.io.makedirs(root / layout.index_dir(self.camera_id))
            self._write_index("returned")
        self._event("audit" if not failed else "warning", "archive_drive_cleared",
                    "Recordings moved to the office were deleted from the archive drive",
                    {"drive_id": drive_id, "deleted": deleted, "not_deleted": failed[:20],
                     "not_deleted_count": len(failed), **({"resumed": True, "returned_utc": resumed.get(
                         "returned_utc") or resumed.get("ts_utc")} if resumed is not None else {})})
        return deleted, failed

    def _leftover_paths(self, drive_id: str, root: Path) -> list[str]:
        """Copies recorded as TRANSFERRED (the office has them) that are still on the drive with their recorded
        size. A path that is also a VERIFIED copy on this drive is never included."""
        assert self.al is not None
        verified = {c["path"] for c in self.al.copies_on(drive_id)}
        out = []
        for r in self.al.transferred_on(drive_id):
            if r["path"] in verified:
                continue
            try:
                st = os.lstat(root / r["path"])
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_size == r["size_bytes"]:
                out.append(r["path"])
        return out

    def _finish_interrupted_returns(self, scan: DriveScan) -> None:
        """A drive back from the office whose return to service was recorded but interrupted before the drive was
        cleared (power cut, process stopped): finish it when the drive is inserted, without a new request."""
        if not self._awaiting or self.al is None:
            self._return_checked.clear()
            return
        present: dict[str, ExportTarget] = {}
        for t in scan.candidates:
            try:
                present[t.drive_key] = t
            except TargetError:
                continue
        self._return_checked &= set(present)
        for k, t in sorted(present.items()):
            if k not in self._awaiting or k in self._return_checked or k == self._swapped_drive:
                continue
            try:
                done = self.al.return_since_swap(k)   # None while the drive's latest swap is newer than any return
                if done is None or self.al.copies_on(k):   # a VERIFIED copy on it: not a return cut short; leave it
                    self._return_checked.add(k)
                    continue
                deleted, failed = self._finish_return(k, t, done)
                self._return_checked.add(k)
            except OSError as exc:            # looked at again at the next scan
                log.warning("could not finish the interrupted return of %s: %s", k, exc)
                continue
            log.info("finished the interrupted return to service of %s: %d deleted, %d not deleted", k, deleted,
                     len(failed))

    def _clean_leftovers(self, root: Path, drive_id: str) -> None:
        """On attach (and at each index write while some remain): delete copies recorded as TRANSFERRED that a
        return could not delete. They are listed in no index, so the office check would flag them NOT LISTED and
        refuse the drive for ever (review 2026-11-12). Never fatal."""
        try:
            paths = self._leftover_paths(drive_id, root)
            if not paths:
                self._leftover_drives.discard(drive_id)
                return
            deleted, failed = self._clear_drive(root, paths, whole=False)
        except Exception as exc:  # noqa: BLE001 - leftovers are retried; copying must go on
            log.warning("could not delete the transferred files left on %s: %s", drive_id, exc)
            self._leftover_drives.add(drive_id)
            return
        if failed:
            self._leftover_drives.add(drive_id)
        else:
            self._leftover_drives.discard(drive_id)
        self._event("audit" if not failed else "warning", "archive_drive_cleared",
                    "Recordings moved to the office that were left on the archive drive were deleted",
                    {"drive_id": drive_id, "deleted": deleted, "not_deleted": failed[:20],
                     "not_deleted_count": len(failed), "leftovers": True})

    def _clear_drive(self, root: Path, paths: list[str], whole: bool = True) -> tuple[int, list[str]]:
        """Delete the transferred copies, the index and playlists (they describe files no longer there) and
        files set aside after a failed read-back; then empty folders. Nothing else is touched.
        ``whole=False``: only the given transferred copies (the drive is in service; its index stays)."""
        cam = root / layout.ROOT / layout.safe(self.camera_id)
        targets = [root / x for x in paths]
        if whole:
            targets += [root / x for x in self.index_names.values()]
            targets.append(root / layout.chain_heads_path(self.camera_id))
            if cam.is_dir():
                targets += [p for p in cam.rglob("*") if p.is_file() and (
                    p.suffix == layout.PLAYLIST_SUFFIX or layout.SET_ASIDE_MARK in p.name)]
        deleted, failed = 0, []
        for p in targets:
            try:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)    # copies are sealed read-only
                p.unlink()
                deleted += 1
            except FileNotFoundError:
                continue
            except OSError:
                failed.append(p.relative_to(root).as_posix())
        if whole and cam.is_dir():
            for d in sorted((x for x in cam.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
                try:
                    d.rmdir()               # only empty folders
                except OSError:
                    pass
        return deleted, failed

    # ------------------------------------------------------------------ swapped drive (persistent)
    def _set_swapped(self, drive_id: str | None, label: str | None = None) -> None:
        """The drive made safe to remove is kept in a small file so that a restart of the archive process
        before it is unplugged does not start writing to it again (review 2026-11-10)."""
        self._swapped_drive = drive_id
        self._swapped_label = label if drive_id is not None else None
        self._last_publish = float("-inf")     # the drives shown changed: publish at the next chance
        p = self.control_dir / SWAPPED_STATE
        try:
            if drive_id is None:
                p.unlink(missing_ok=True)
            else:
                self.control_dir.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name(p.name + PARTIAL)
                tmp.write_text(json.dumps({"drive_id": drive_id, "label": label, "swapped_utc": iso_utc(time.time()),
                                           "ejected": bool(self._swap_result and self._swap_result[0])}), "utf-8")
                os.replace(tmp, p)
        except OSError:
            log.exception("could not record the swapped archive drive")

    def _load_swapped(self) -> None:
        p = self.control_dir / SWAPPED_STATE
        try:
            doc = json.loads(p.read_text("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("swapped-drive record unreadable; ignored")
            return
        drive_id = doc.get("drive_id") if isinstance(doc, dict) else None
        if not isinstance(drive_id, str) or not drive_id:
            return
        self._swapped_drive = self._last_swapped = drive_id
        label = doc.get("label")
        self._swapped_label = label if isinstance(label, str) and label else None
        self._swapped_absent_polls = 0
        self._swap_result = (bool(doc.get("ejected")), "prepared for removal before the archive process restarted")
        self.after_swap = True
        log.info("archive drive %s was prepared for removal before this start; not writing to it", drive_id)

    def _clear_swap(self) -> None:
        self._swap.clear()
        self._swap_deadline = None

    def _drop_swap(self, why: str) -> None:
        """A swap request that cannot be about any drive that will be attached next (review 2026-11-12)."""
        self._clear_swap()
        log.info("swap request dropped: %s", why)
        self._event("info", "archive_swap_dropped", "A swap request was dropped: nothing was prepared for removal",
                    {"why": why})
        self._last_publish = float("-inf")
        self._wake.set()

    def _set_awaiting(self, drive_id: str | None, label: str | None = None, *, remove: bool = False) -> None:
        """Drives taken out with Swap and not yet returned to service (persisted). ``None`` clears all (not
        used by Cancel swap, which releases only drives inserted now); ``remove`` clears one drive (returned to
        service, or Cancel swap)."""
        if drive_id is None:
            self._awaiting.clear()
            self._awaiting_since.clear()
        elif remove:
            self._awaiting.pop(drive_id, None)
            self._awaiting_since.pop(drive_id, None)
        else:
            self._awaiting[drive_id] = label
            self._awaiting_since[drive_id] = time.time()
        self._last_publish = float("-inf")
        p = self.control_dir / AWAITING_STATE
        try:
            if not self._awaiting:
                p.unlink(missing_ok=True)
            else:
                self.control_dir.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name(p.name + PARTIAL)
                tmp.write_text(json.dumps({"drives": self._awaiting, "since": self._awaiting_since}), "utf-8")
                os.replace(tmp, p)
        except OSError:
            log.exception("could not record the archive drives waiting to be returned to service")

    def _load_awaiting(self) -> None:
        p = self.control_dir / AWAITING_STATE
        try:
            doc = json.loads(p.read_text("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("awaiting-return record unreadable; ignored")
            return
        drives = doc.get("drives") if isinstance(doc, dict) else None
        if isinstance(drives, dict):
            self._awaiting = {str(k): (v if isinstance(v, str) else None) for k, v in drives.items() if k}
        since = doc.get("since") if isinstance(doc, dict) else None
        if isinstance(since, dict):
            self._awaiting_since = {str(k): float(v) for k, v in since.items()
                                    if k in self._awaiting and isinstance(v, (int, float))}

    def _awaiting_text(self) -> str | None:
        if not self._awaiting_present:
            return None
        names = ", ".join(sorted(str(t.label or self._awaiting.get(k) or "An archive drive")
                                 for k, t in self._awaiting_present.items()))
        return (f"{names} was taken out with Swap archive drive, so Sentinel does not copy to it until it is "
                "returned to service (Drives › Return to service…, after the office has copied it). "
                "To keep using it without an office copy, press Cancel swap.")

    def _has_room(self, t: ExportTarget) -> bool:
        """Room on a drive that is not in use for the next recording (at least one file at the measured rate)."""
        try:
            free = self.io.free_bytes(Path(t.root))
        except OSError:
            return False
        need = 0
        if self.al is not None:
            nxt = self.al.pending_segments(t.drive_key, limit=1)
            need = int(nxt[0]["size_bytes"] or 0) if nxt else 0
            rate = self.al.recording_rate()
            if rate:
                need = max(need, int(rate * self.cfg.recording.segment_seconds))
        return free >= max(need, 1) + self.reserve

    def _idle_full(self) -> ExportTarget | None:
        """A full archive drive still in the dock while copying goes to another one."""
        for k in sorted(self._present):
            if k in self._full_drives and (self.att is None or k != self.att.drive_id):
                return self._present[k]
        return None

    def _do_swap(self) -> str:
        idle = self._idle_full()
        if idle is not None and self.att is not None and not self.full:
            return self._swap_idle(idle)
        att = self.att
        assert att is not None
        self._set(ST_SWAP_PENDING, "Preparing the archive drive for removal. Do not unplug it yet.")
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
        self._swap_result = (res.ejected, res.detail)
        self._set_swapped(att.drive_id, att.target.label)
        self._set_awaiting(att.drive_id, att.target.label)
        self._full_drives.pop(att.drive_id, None)
        self._present.pop(att.drive_id, None)
        self._last_swapped = att.drive_id
        self._swapped_absent_polls = 0
        self.after_swap = True
        self.att = None
        self._event("audit", "archive_swap_ready", "Archive drive prepared for removal",
                    {"drive_id": att.drive_id, "ejected": res.ejected, "detail": res.detail,
                     "last_copy_id": self.al.last_copy_id(att.drive_id) if self.al else None})
        self._waiting_for_removal()
        return "swapped"

    def _swap_idle(self, t: ExportTarget) -> str:
        """Swap with two drives: prepare the FULL drive (its index was written when copying moved on) for
        removal; copying carries on to the drive in use."""
        key = t.drive_key
        self._set(ST_SWAP_PENDING, f"Preparing {t.label or 'the full archive drive'} for removal.")
        self.remover.flush(t)
        res = self.remover.eject(t)
        self._clear_swap()
        self._swap_result = (res.ejected, res.detail)
        self._set_swapped(key, t.label)
        self._set_awaiting(key, t.label)
        self._full_drives.pop(key, None)
        self._present.pop(key, None)
        self._last_swapped = key
        self._swapped_absent_polls = 0
        self._event("audit", "archive_swap_ready", "Archive drive prepared for removal",
                    {"drive_id": key, "ejected": res.ejected, "detail": res.detail, "full": True,
                     "last_copy_id": self.al.last_copy_id(key) if self.al else None,
                     "copying_continues_on": self.att.drive_id if self.att else None})
        if self.att is not None:          # copying carries on: show that, plus which drive to take out
            self._set(ST_COPYING, "Copying recordings to the archive drive. Do not unplug it.")
        self._publish(force=True)
        return "swapped"

    def _removal_text(self) -> str | None:
        """What to do with the drive prepared for removal, while another drive may be in use."""
        if self._swapped_drive is None:
            return None
        name = self._swapped_label or "The archive drive"
        if self._swap_result and self._swap_result[0]:
            return f"{name} is safe to remove."
        if self.att is not None or self._present:
            return (f"{name} is ready to be taken out: Sentinel has finished writing to it. Take out only that "
                    f"drive. Do not use Windows' \"Safely Remove Hardware\" for it: in a two-bay dock that would "
                    "also stop the other drive.")
        return None

    def _rollover_text(self) -> str | None:
        idle = self._idle_full()
        if idle is None or self.att is None:
            return None
        return (f"{idle.label or 'One archive drive'} is full; copying continues on "
                f"{self.att.target.label or 'the other drive'}. Press Swap Archive Drive to take the full drive out.")

    def _waiting_for_removal(self) -> None:
        ejected = bool(self._swap_result and self._swap_result[0])
        if ejected:
            self._set(ST_SAFE, "SAFE TO REMOVE ARCHIVE DRIVE.")
        elif self._present:
            self._set(ST_REMOVE_MANUALLY, self._removal_text() or "Take out the archive drive.")
        elif os.name == "nt":
            self._set(ST_REMOVE_MANUALLY, "Do not unplug the archive drive yet. Click the \"Safely Remove "
                                          "Hardware\" icon near the clock, choose this drive, and unplug it only "
                                          "after Windows says it is safe to remove.")
        else:
            self._set(ST_REMOVE_MANUALLY, "Do not unplug the archive drive yet. Use the \"Eject\" or \"Safely "
                                          "remove\" option for this drive and unplug it only after it has "
                                          "disappeared.")

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
            drive_id = self.att.drive_id
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
            except UnverifiableCopy as exc:
                self._skip.add(seg["segment_id"])
                self.al.add_problem(drive_id, "unverifiable", str(exc), seg["segment_id"])
                self._event("error", "archive_unverifiable", "Archive copies on this drive cannot be checked "
                            "without the operating-system cache", {"segment_id": seg["segment_id"], "detail": str(exc)})
                return
            except VerifyMismatch as exc:
                key = f"{seg['segment_id']}|{drive_id}|{kind}"
                n = self._verify_attempts.get(key, 0) + 1
                self._verify_attempts[key] = n
                self._event("error", "archive_verify_failed", "An archive copy did not verify",
                            {"segment_id": seg["segment_id"], "kind": kind, "attempt": n, "detail": str(exc)})
                if n >= self.acfg.verify_attempts:
                    self._skip.add(seg["segment_id"])
                    self.al.add_problem(drive_id, "verify_failed", str(exc), seg["segment_id"])
                return
            except DriveFull as exc:
                self._drive_full(str(exc))
                return
            except OSError as exc:
                if self._drive_error(exc):
                    return
                # First review run, 2026-11-12: a file on the drive that cannot be read back (bad sector) raised
                # the same error at every step, so nothing else was ever copied (and A-007 said "not running").
                # After the usual attempts it is handled like a copy that does not verify: recorded as a problem
                # (A-006) and skipped, so later recordings keep flowing.
                key = f"io|{seg['segment_id']}|{drive_id}|{kind}"
                n = self._verify_attempts.get(key, 0) + 1
                self._verify_attempts[key] = n
                if n < self.acfg.verify_attempts:
                    raise
                self._skip.add(seg["segment_id"])
                detail = f"{type(exc).__name__}: {exc}"
                self.al.add_problem(drive_id, "verify_failed", detail, seg["segment_id"])
                self._event("error", "archive_verify_failed", "An archive copy could not be read back from the "
                            "archive drive", {"segment_id": seg["segment_id"], "kind": kind, "attempt": n,
                                              "detail": detail})
                return

    def _drive_error(self, exc: OSError) -> bool:
        """Classify an error on the archive drive. True if handled (removed / full)."""
        att = self.att
        if att is not None and not self.io.root_present(Path(att.target.root)):
            self._detached(unexpected=True)
            self._set(ST_ABSENT, MSG_REMOVED)
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
        if self.att is not None:
            self._full_drives.setdefault(self.att.drive_id, self.mono())
        self._set(ST_FULL, MSG_FULL)

    def _roll_over(self, nxt: ExportTarget) -> None:
        """The drive in use is full and another archive drive with room is inserted: finish the full drive
        (index written and read back, recording-log fingerprint, flush) and leave it, without treating it as
        removed. It stays in the dock until the operator swaps it."""
        att = self.att
        assert att is not None
        try:
            self._write_index("full")
        except Exception:  # noqa: BLE001 - the next drive must still be used
            log.exception("could not write the index of the full archive drive")
        if self.att is not None:
            try:
                self.remover.flush(att.target)
            except Exception:  # noqa: BLE001
                log.exception("could not flush the full archive drive")
        self._event("info", "archive_drive_rollover", "Archive drive full; copying continues on the next archive "
                    "drive", {"full_drive_id": att.drive_id, "full_label": att.target.label,
                              "next_drive_id": nxt.drive_key, "next_label": nxt.label,
                              "copies_this_attach": att.copies_this_attach})
        log.info("archive drive %s is full; moving on to %s", att.drive_id, nxt.drive_key)
        self._last_publish = float("-inf")
        if self.att is not None:
            self._detached(unexpected=False)

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

    def _heartbeat(self) -> None:
        """Keep the status file fresh during a long copy or read-back (field report 2026-11-11: with
        one-hour files the window showed "Archive copying is not running" while a copy was going on)."""
        now = self.mono()
        if now - self._last_heartbeat < HEARTBEAT_S:
            return
        self._last_heartbeat = now
        try:
            self.status.write()
        except Exception:  # noqa: BLE001 - a heartbeat must never stop a copy
            log.exception("could not refresh the archive status heartbeat")

    def _should_abort(self) -> bool:
        self._heartbeat()
        if self._stop.is_set():
            return True
        if not self._swap.is_set():
            self._poll_swap_in_flight()
        return self._swap.is_set() and self._swap_deadline is not None and self.mono() > self._swap_deadline

    def _poll_swap_in_flight(self) -> None:
        """(review 2026-11-10) In production the swap request arrives as a file; look for it while a copy
        is in progress too, so that swap_max_wait_s applies and the status shows the swap at once."""
        now = self.mono()
        if now - self._last_control_poll < CONTROL_POLL_S:
            return
        self._last_control_poll = now
        if self._take_request(SWAP_REQUEST):
            self.request_swap()
            self._set(ST_SWAP_PENDING, "Preparing the archive drive for removal. Do not unplug it yet.")

    def _read_hash(self, path: Path) -> tuple[str, int, str]:
        h = hashlib.sha256()
        n = 0
        chunks, method = self.io.read_back(path)
        for c in chunks:
            h.update(c)
            n += len(c)
            self.verify_throttle.consume(len(c))
            self._heartbeat()
        return h.hexdigest(), n, method

    def _set_aside(self, dest: Path, rel: str, seg: Any, found: str) -> Path:
        assert self.att is not None and self.al is not None
        aside = self.io.rename_aside(dest, f"{layout.SET_ASIDE_MARK}{compact_utc(time.time())}")
        aside_rel = rel.rsplit("/", 1)[0] + "/" + aside.name
        self.al.add_problem(self.att.drive_id, "set_aside", f"read back {found}; kept as {aside.name}",
                            seg["segment_id"], aside_rel)
        return aside

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
            aside = self._set_aside(dest, rel, seg, sha)
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
            aside = self._set_aside(dest, rel, seg, sha)
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
        """Order matters: (1) refuse a cache-served read-back, (2) make the rename durable, (3) seal
        read-only, (4) journal line on the drive (flushed), (5) ledger row. A removal between any two
        steps leaves either nothing claimed (re-verified on return) or a journal line for a copy that
        IS verified on the drive (reconciled when it returns)."""
        att = self.att
        assert att is not None and self.al is not None
        if method == M_BUFFERED:
            raise UnverifiableCopy(f"read-back of {rel} could not bypass the operating-system cache")
        self.io.confirm_in_place(dest)
        self.io.seal_read_only(dest)
        verified_utc = iso_utc(time.time())
        journal = Path(att.target.root) / self.index_names["journal"]
        if not journal.exists():
            self.io.append_line(journal, idx.JOURNAL_HEADER)
        self.io.append_line(journal, idx.journal_line(verified_utc, self.camera_id, seg["segment_id"], kind, sha, n,
                                                      rel))
        self.al.record_verified(segment_id=seg["segment_id"], drive_id=att.drive_id, kind=kind, path=rel,
                                sha256=sha, size_bytes=n, verify_method=method, already_present=already_present)
        att.copies_since_index += 1
        att.copies_this_attach += 1
        if kind in layout.MEDIA_KINDS:
            att.footage_since_index += float(seg["duration_s"] or 0.0)
        self._publish()

    # ================================================================== index
    def _read_drive_file(self, p: Path) -> bytes | None:
        try:
            return p.read_bytes()
        except FileNotFoundError:
            return None

    def _write_index(self, reason: str) -> bool:
        att = self.att
        assert att is not None and self.al is not None and self.ledger is not None
        root = Path(att.target.root)
        if reason != "attach" and att.drive_id in self._leftover_drives:
            self._clean_leftovers(root, att.drive_id)     # before a swap index above all: the office checks it
        try:
            prev = idx.previous_entries(self._read_drive_file(root / self.index_names["json"]),
                                        self._read_drive_file(root / self.index_names["journal"]))
        except OSError as exc:
            if self._drive_error(exc):
                return False
            prev = {}
        copies = self.al.copies_on(att.drive_id)
        known = self.al.known_paths(att.drive_id)
        carried = []
        for path, e in sorted(prev.items()):
            if path in known:
                continue
            try:
                ok = (root / path).stat().st_size == int(e.get("size_bytes") or -1)
            except (OSError, ValueError):
                ok = False
            if ok:
                carried.append(dict(e, source="carried_forward"))
        set_aside = [{"path": r["path"], "segment_id": r["segment_id"], "detail": r["detail"],
                      "created_utc": r["created_utc"]} for r in self.al.set_aside_on(att.drive_id)]
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
        files = idx.build_index(camera_id=self.camera_id, generation=gen, generated_utc=generated,
                                ledger_instance=self._instance or "", tz=self.tz, drive=drive,
                                software={"app_version": __version__}, policy=self.acfg.resume_policy,
                                copies=copies, gaps=gaps, not_on_drive=not_on, carried=carried, set_aside=set_aside)
        n = self.index_names
        try:
            # data files first, SHA256SUMS last; each file is complete and correct on its own
            for rel in (n["json"], n["csv"], n["gaps"], n["sums"]):
                self.io.replace_file(root / rel, files[rel], verify=lambda p: self._read_hash(p)[0])
        except OSError as exc:
            log.warning("archive index write failed: %s", exc)
            att.stale_index = True
            att.next_index_retry = self.mono() + 60.0
            if not self._drive_error(exc):
                self._event("warning", "archive_index_failed", "Archive drive index could not be written",
                            {"error": f"{type(exc).__name__}: {exc}"})
            return False
        self.al.record_index(att.drive_id, gen, generated, reason, len(copies),
                             hashlib.sha256(files[n["json"]]).hexdigest(), hashlib.sha256(files[n["sums"]]).hexdigest())
        att.footage_since_index = 0.0
        att.copies_since_index = 0
        att.last_index_mono = self.mono()
        att.stale_index = False
        log.info("archive index generation %d written to %s (%s, %d files)", gen, att.drive_id, reason, len(copies))
        self._anchor_on_drive(root, reason)
        self._write_playlists(root, [dict(c) for c in copies] + carried)
        return True

    def _write_playlists(self, root: Path, entries: list[dict[str, Any]]) -> None:
        """One playlist per local day (owner request 2026-11-11). A playlist only points at verified copies
        on this drive; it is not evidence, so a failure is logged and never stops archiving. Unchanged
        playlists are not rewritten."""
        try:
            files = idx.build_playlists(self.camera_id, entries, self.tz)
            if files:
                self.io.makedirs(root / layout.playlist_dir(self.camera_id))
            for rel, data in files.items():
                if self._read_drive_file(root / rel) == data:
                    continue
                self.io.replace_file(root / rel, data, verify=lambda p: self._read_hash(p)[0])
        except Exception as exc:  # noqa: BLE001 - playlists are a convenience
            log.warning("could not write the archive playlists: %s", exc)

    def _anchor_on_drive(self, root: Path, reason: str) -> None:
        """Copy the recording log's fingerprint (chain head) onto the archive drive, append-only, every
        time its index is written (attach, hourly, swap). A drive that has left the building then
        proves what the log held at that moment: rewriting the laptop's log later no longer matches
        it (``Sentinel ledger verify --anchors-from <drive>``). Never stops archiving if it fails."""
        from ..ledgerchain import append_anchor, current_chain_head

        assert self.ledger is not None
        try:
            head = current_chain_head(self.ledger)
            if head is None:
                return
            line = append_anchor(root / layout.chain_heads_path(self.camera_id), head, f"archive_{reason}")
            log.info("recording log fingerprint written to the archive drive: %s", line)
        except Exception as exc:  # noqa: BLE001 - tamper evidence is extra; never block archiving
            log.warning("could not write the recording log fingerprint to the archive drive: %s", exc)

    # ================================================================== status
    def _set(self, state: str, message: str) -> None:
        changed = state != self.state
        self.state, self.message = state, message
        self._publish(force=changed)

    def conditions(self) -> tuple[Condition, Any, Any, list[Any]]:
        assert self.al is not None
        risk = self.al.backlog(None)
        queue = self.al.backlog(self.att.drive_id) if self.att is not None else None
        problems = self.al.open_problems()
        cond = Condition(state=self.state, backlog_s=risk.seconds, threshold_s=self.acfg.backlog_alert_hours * 3600,
                         removed_unexpectedly=self.removed_unexpectedly, after_swap=self.after_swap, full=self.full,
                         unusable=self.state == ST_UNUSABLE, verify_failed=bool(problems),
                         source_unreadable=bool(self.source_failed), software_errors=self.errors,
                         capacity_low=self._capacity_low())
        return cond, risk, queue, problems

    def _capacity_low(self) -> bool:
        """The attached drive has less free space than ``capacity_warning_days`` of footage at the
        measured recording rate (alert A-009)."""
        if self.att is None or self.al is None:
            return False
        rate = self.al.recording_rate()
        if not rate:
            return False
        try:
            free = self.io.free_bytes(Path(self.att.target.root))
        except OSError:
            return False
        return free < self.acfg.capacity_warning_days * 86400 * rate

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
        cond, risk, queue, problems = self.conditions()
        alerts = evaluate_alerts(cond)
        drive = ("full" if self.full else "present") if self.att is not None or self._swapped_drive else (
            "unusable" if self.state == ST_UNUSABLE else "absent")
        text = backlog_text(risk.seconds)
        st = self.stats
        notes = [] if self.state in (ST_SAFE, ST_REMOVE_MANUALLY) else [
            x for x in (self._removal_text(), self._rollover_text(),
                        self._awaiting_text() if self.state != ST_ABSENT else None) if x]
        message = " ".join(notes + [f"{self.message} {text}."])
        self.status.write(
            state=self.state, drive=drive, message=message, camera_id=self.camera_id,
            drives=self._drives_status(),
            awaiting_return=[{"drive_id": k, "label": t.label or self._awaiting.get(k)}
                             for k, t in sorted(self._awaiting_present.items())],
            removal=None if self._swapped_drive is None else {
                "drive_id": self._swapped_drive, "label": self._swapped_label,
                "safe": bool(self._swap_result and self._swap_result[0]), "text": self._removal_text()},
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
            problems={"open": [{"problem_id": p["problem_id"], "drive_id": p["drive_id"], "kind": p["kind"],
                                "path": p["path"], "created_utc": p["created_utc"]} for p in problems[:20]],
                      "source_unreadable": sorted(self.source_failed)[:20], "unusable_reason": self.unusable_reason,
                      "other_drives": [r for _root, r in (self._last_scan[0].ignored if self._last_scan[0] else [])][:10]},
            priority=self.priority, cpu_process_s=round(time.process_time(), 2))

    def _drives_status(self) -> list[dict[str, Any]]:
        """Every archive drive inserted: in use, full (waiting to be swapped) or standing by."""
        out: list[dict[str, Any]] = []
        present = dict(self._present)
        if self.att is not None:
            present[self.att.drive_id] = self.att.target
        for k, t in sorted(present.items()):
            in_use = self.att is not None and k == self.att.drive_id
            try:
                free: int | None = int(self.io.free_bytes(Path(t.root)))
            except (OSError, ValueError):
                free = None
            out.append({"drive_id": k, "label": t.label, "use": "in_use" if in_use else (
                "full" if k in self._full_drives else "standby"),
                "full": k in self._full_drives or (in_use and self.full), "free_bytes": free})
        if self._swapped_drive is not None:
            out.append({"drive_id": self._swapped_drive, "label": self._swapped_label, "use": "take_out",
                        "full": False, "free_bytes": None})
        for k, t in sorted(self._awaiting_present.items()):
            out.append({"drive_id": k, "label": t.label or self._awaiting.get(k), "use": "awaiting_return",
                        "full": False, "free_bytes": None})
        return out

    def _event(self, severity: str, code: str, message: str, detail: dict[str, Any] | None) -> None:
        try:
            self.al.event(severity, code, message, detail)  # type: ignore[union-attr]
        except Exception:
            log.exception("could not record archive event %s", code)


def operator_request_dir(cfg: AppConfig) -> Path:
    """Folder where the operator window (standard user, "Sentinel Operators") asks for a swap / resume:
    ``<[storage] status_dir>/requests``; without a separate status folder, the service control folder."""
    if cfg.storage.status_dir:
        return Path(cfg.storage.status_dir) / OPERATOR_REQUESTS
    return cfg.data_path / CONTROL_DIR


def _write_request(cfg: AppConfig, name: str, operator: bool) -> Path:
    d = operator_request_dir(cfg) if operator else cfg.data_path / CONTROL_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(iso_utc(time.time()), "utf-8")
    return p


def request_swap_file(cfg: AppConfig, operator: bool = False) -> Path:
    """Used by the CLI / GUI in another process: ask the running archive worker to swap.
    ``operator=True``: the window, written to :func:`operator_request_dir`."""
    return _write_request(cfg, SWAP_REQUEST, operator)


def request_resume_file(cfg: AppConfig, operator: bool = False) -> Path:
    """Cancel a swap / keep using the same drive (see :func:`request_swap_file`)."""
    return _write_request(cfg, RESUME_REQUEST, operator)
