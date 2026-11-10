"""Review-drive export: CHECKING → PLANNING → COPYING → VERIFYING → MANIFEST → EJECTING → COMPLETE.

See docs/04 §6 and :mod:`sentinel.export.ledger` for the state machine. Every outcome is one
of: COMPLETE (every file read back and verified, manifests written and read back), FAILED
(needs a person; operator alert E-0xx), INTERRUPTED (the drive vanished; resumed when the same
drive is presented again), or nothing to export.

Rules enforced here:
* local recordings are only read, never modified or deleted;
* a file that already exists on the drive is never overwritten: same content → re-verified
  and recorded (``already_present``), different content → explicit conflict (E-004);
* originals are verified against the SHA-256 recorded when they were sealed; MP4 derivatives
  are verified by decoding the bytes read back from the drive and comparing the per-frame
  decoded SHA-256 list with the concatenation of the sources' lists. An MP4's file hash is
  never compared with, or reported as, an original's hash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .. import __version__
from ..alerts import Severity, make_alert
from ..clock import iso_utc
from ..config import AppConfig
from ..db import Ledger
from ..media.probe import ffmpeg_version as probe_ffmpeg_version
from ..media.videohash import METHOD_PREFIX, short_version
from ..platform import get_platform
from ..platform.base import InstanceLockError, Platform
from . import layout
from .fileio import PARTIAL, SourceMismatch, TargetIO, is_disk_full, partial_path
from .ledger import (B_COMPLETE, B_COPYING, B_EJECTING, B_FAILED, B_INTERRUPTED, B_MANIFEST, B_VERIFYING,
                     I_COPIED, I_FAILED, I_PLANNED, I_VERIFIED, ExportLedger, NewItem)
from .manifest import ManifestBuilder, sha256_bytes
from .media import (MediaError, frame_hashes_from_concat, frame_hashes_from_path, frame_hashes_from_stream,
                    remux_concat)
from .planner import PlanError, Planner
from .status import STATUS_FILE, ExportStatusWriter
from .target import ExportTarget, ManualRemovalFallback, SafeRemover, TargetError, same_volume

log = logging.getLogger(__name__)

WRITE_PROBE = ".sentinel_write_test" + PARTIAL


class ExportFailure(Exception):
    def __init__(self, code: str, detail: str, interrupted: bool = False, item_id: int | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.interrupted = interrupted
        self.item_id = item_id


class SourceReadError(OSError):
    """Reading a local recording failed (distinguished from errors on the export drive)."""


@dataclass
class ExportResult:
    state: str                  # COMPLETE | FAILED | INTERRUPTED | NOTHING_TO_EXPORT
    batch_id: str | None
    message: str
    code: str | None = None
    ejected: bool = False
    detail: str | None = None


def content_hash_of(frames: list[str]) -> str:
    """Same formula as ``videohash.aggregate``: SHA-256 over newline-joined per-frame digests."""
    return hashlib.sha256("\n".join(frames).encode("ascii")).hexdigest()


class ReviewExporter:
    def __init__(self, cfg: AppConfig, target: ExportTarget, *, ledger: Ledger | None = None,
                 io: TargetIO | None = None, remover: SafeRemover | None = None, platform: Platform | None = None,
                 operator_id: str | None = None, status_path: Path | None = None,
                 ffmpeg_version: str | None = None, allow_same_volume: bool = False) -> None:
        self.cfg = cfg
        self.target = target
        self.root = Path(target.root)
        self._own_ledger = ledger is None
        self.ledger = ledger
        self.io = io or TargetIO()
        self.remover: SafeRemover = remover or ManualRemovalFallback()
        self.platform = platform or get_platform()
        self.operator_id = operator_id
        self.status = ExportStatusWriter(status_path or (cfg.data_path / STATUS_FILE))
        self._ffmpeg_version = ffmpeg_version
        self.allow_same_volume = allow_same_volume
        self.tz = ZoneInfo(cfg.recording.display_timezone)
        self.camera_id = cfg.camera.id
        self.tmp_dir = cfg.data_path / "export_tmp"
        self.bytes_total = 0
        self.bytes_done = 0
        self.batch_id: str | None = None
        self.drive_id: str | None = None
        self._phase = "CHECKING"

    # ------------------------------------------------------------------ public
    def run(self) -> ExportResult:
        try:
            lock = self.platform.acquire_instance_lock(self.cfg.data_path / "export.lock")
        except InstanceLockError:
            msg = "Another export is already running. Wait for it to finish."
            self.status.write("FAILED", msg, alert=None)
            return ExportResult("FAILED", None, msg, code=None, detail="export lock held")
        try:
            if self.ledger is None:
                self.ledger = Ledger(self.cfg.data_path / "sentinel.db")
            self.xl = ExportLedger(self.ledger)
            if self._ffmpeg_version is None:
                self._ffmpeg_version = probe_ffmpeg_version(self.cfg.tools.ffmpeg)
            return self._run()
        finally:
            try:
                lock.close()
            except Exception:
                pass
            if self._own_ledger and self.ledger is not None:
                self.ledger.close()
                self.ledger = None

    # ------------------------------------------------------------------ top level
    def _run(self) -> ExportResult:
        self._status("CHECKING", "Checking the review drive. Please wait.")
        try:
            self._check_target()
            self.drive_id = self.xl.upsert_drive(self.target)
        except ExportFailure as f:
            return self._fail_without_batch(f)
        except TargetError as exc:
            return self._fail_without_batch(ExportFailure("E-005", str(exc)))
        batch = self.xl.resumable_batch(self.camera_id, self.drive_id)
        if batch is not None:
            self.batch_id = batch["batch_id"]
            self.xl.bump_attempts(self.batch_id)
            self._event("info", "export_resumed", "Review-drive export resumed on the same drive",
                        {"previous_state": batch["state"], "error_code": batch["error_code"]})
            log.info("resuming export %s (was %s)", self.batch_id, batch["state"])
        else:
            self._status("PLANNING", "Preparing the list of recordings to export.")
            try:
                plan = Planner(self.ledger, self.cfg.spool_path, self.camera_id, self.tz).plan(  # type: ignore[arg-type]
                    self.drive_id, self.xl.latest_complete_end(self.camera_id), self.io.free_bytes(self.root))
            except PlanError as exc:
                return self._fail_without_batch(ExportFailure(exc.code, f"{exc} {json.dumps(exc.detail)}"))
            except OSError as exc:
                return self._fail_without_batch(self._classify(exc))
            if plan is None:
                msg = "There are no new recordings to export. Nothing was written to the drive."
                self._status("NOTHING_TO_EXPORT", msg, percent=100)
                return ExportResult("NOTHING_TO_EXPORT", None, msg)
            now = time.time()
            self.batch_id = self.xl.create_batch(
                camera_id=self.camera_id, drive_id=self.drive_id, interval_start_utc=plan.interval_start_utc,
                interval_end_utc=plan.interval_end_utc, display_timezone=self.cfg.recording.display_timezone,
                operator_id=self.operator_id, app_version=__version__, ffmpeg_version=self._ffmpeg_version,
                stamp=layout.export_stamp(now, self.tz), plan=plan.to_json(), items=plan.items)
            self._event("info", "export_started", "Review-drive export started",
                        {"interval_start_utc": plan.interval_start_utc, "interval_end_utc": plan.interval_end_utc,
                         "items": len(plan.items), "planned_bytes": plan.planned_bytes,
                         "operator_id": self.operator_id})
        return self._execute()

    def _execute(self) -> ExportResult:
        bid = self.batch_id
        assert bid is not None
        try:
            self.xl.set_batch_state(bid, B_COPYING, error_code=None, error_detail=None)
            items = self.xl.items(bid)
            self.bytes_total = 2 * sum(int(i["planned_bytes"] or 0) for i in items if i["kind"] != "document")
            self.bytes_done = 0
            for it in items:
                if it["state"] != I_VERIFIED:
                    self.io.discard_partial(self.root / it["dest_rel_path"])
            self._phase = "COPYING"
            for it in items:
                self._copy_phase_item(it)
            self.xl.set_batch_state(bid, B_VERIFYING)
            self._phase = "VERIFYING"
            for it in self.xl.items(bid):
                if it["state"] == I_COPIED:
                    self._verify_item(it)
            self.xl.set_batch_state(bid, B_MANIFEST)
            self._status("MANIFEST", "Writing the list of files and the summary. Do not remove the drive.",
                         percent=99)
            manifest_rel = self._write_documents()
            self.xl.set_batch_state(bid, B_EJECTING)
            self._status("EJECTING", "Finishing. Do not remove the drive.", percent=99)
            self.remover.flush(self.target)
            res = self.remover.eject(self.target)
            self.xl.set_batch_state(bid, B_COMPLETE, ejected=int(res.ejected), removal_detail=res.detail)
            self._event("audit", "export_complete", "Review-drive export complete and verified",
                        {"manifest": manifest_rel, "ejected": res.ejected})
            state = "SAFE_TO_REMOVE" if res.ejected else "COMPLETE_REMOVE_MANUALLY"
            message = ("SAFE TO REMOVE DRIVE. Export complete and verified." if res.ejected else res.operator_message)
            self._status(state, message, percent=100)
            return ExportResult("COMPLETE", bid, message, ejected=res.ejected)
        except ExportFailure as f:
            return self._fail(f)
        except (OSError, MediaError) as exc:
            return self._fail(self._classify(exc))
        except Exception as exc:  # unexpected: never leave the batch looking healthy
            log.exception("export failed unexpectedly")
            return self._fail(ExportFailure("E-007", f"{type(exc).__name__}: {exc}"))

    # ------------------------------------------------------------------ checks / classification
    def _check_target(self) -> None:
        if not self.io.root_present(self.root):
            raise ExportFailure("E-005", "target directory is not present")
        if not self.allow_same_volume and same_volume(self.root, self.cfg.spool_path):
            raise ExportFailure("E-005", "target is on the same volume as the recording storage; refusing "
                                         "(it would consume recording space)")
        probe = self.root / WRITE_PROBE
        try:
            with open(probe, "wb") as fh:
                fh.write(b"\0" * 4096)
                fh.flush()
                os.fsync(fh.fileno())
            probe.unlink()
        except OSError as exc:
            raise self._classify(exc, default="E-005") from None

    def _classify(self, exc: BaseException, default: str = "E-005") -> ExportFailure:
        detail = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, MediaError) and exc.stderr_tail:
            detail += f" | {exc.stderr_tail[-300:]}"
        if not self.io.root_present(self.root):
            return ExportFailure("E-002", detail, interrupted=True)
        if is_disk_full(exc) or (isinstance(exc, MediaError) and exc.disk_full):
            return ExportFailure("E-001", detail)
        if isinstance(exc, SourceReadError):
            return ExportFailure("E-006", detail)
        if isinstance(exc, FileExistsError):
            return ExportFailure("E-004", detail)
        if isinstance(exc, MediaError):
            return ExportFailure("E-007", detail)
        return ExportFailure(default, detail)

    # ------------------------------------------------------------------ copy phase
    def _copy_phase_item(self, it: Any) -> None:
        dest = self.root / it["dest_rel_path"]
        weight = int(it["planned_bytes"] or 0)
        st = it["state"]
        if st == I_VERIFIED:
            if not self.io.exists(dest):
                self.xl.set_item_state(it["item_id"], I_FAILED, error="verified file is missing from the drive")
                raise ExportFailure("E-003", f"previously verified file missing: {it['dest_rel_path']}",
                                    item_id=it["item_id"])
            self._progress(2 * weight)
            return
        if st == I_COPIED:
            if self.io.exists(dest):
                self._progress(weight)
                return
            self.xl.set_item_state(it["item_id"], I_PLANNED, error="copied file missing on resume; copying again")
        elif st == I_FAILED:
            raise ExportFailure("E-007", f"item {it['item_id']} is FAILED inside a resumable batch")
        if it["kind"] == "document":
            return  # regenerated and written at the MANIFEST stage
        if self.io.exists(dest):
            self._existing(it, dest)
            self._progress(2 * weight)
            return
        if it["kind"] == "mp4":
            self._copy_mp4(it, dest)
        else:
            self._copy_file(it, dest)
        self._progress(weight)

    def _source_chunks(self, path: Path) -> Iterator[bytes]:
        try:
            fh = open(path, "rb", buffering=0)
        except OSError as exc:
            raise SourceReadError(exc.errno, f"cannot open local recording: {exc}") from None
        with fh:
            while True:
                try:
                    b = fh.read(1024 * 1024)
                except OSError as exc:
                    raise SourceReadError(exc.errno, f"cannot read local recording: {exc}") from None
                if not b:
                    return
                yield b

    def _copy_file(self, it: Any, dest: Path) -> None:
        src = self.cfg.spool_path / it["source_rel_path"]
        try:
            _sha, n = self.io.write_new(dest, self._source_chunks(src), expected_sha256=it["expected_sha256"])
        except SourceMismatch as exc:
            self.xl.set_item_state(it["item_id"], I_FAILED, error=str(exc))
            self._event("error", "export_source_mismatch", "A local recording no longer matches its sealed hash",
                        {"item_id": it["item_id"], "source": it["source_rel_path"]})
            raise ExportFailure("E-006", str(exc), item_id=it["item_id"]) from None
        except (OSError, MediaError) as exc:
            f = self._classify(exc)
            if not f.interrupted:
                self.xl.set_item_state(it["item_id"], I_FAILED, error=f.detail[:500])
            raise f from None
        self.xl.set_item_state(it["item_id"], I_COPIED, size_bytes=n, copied_utc=iso_utc(time.time()))

    def _copy_mp4(self, it: Any, dest: Path) -> None:
        segs = self._sources(it)
        paths = [self.cfg.spool_path / s["rel_path"] for s in segs]
        for p in paths:
            if not p.exists():
                self.xl.set_item_state(it["item_id"], I_FAILED, error=f"source missing: {p.name}")
                raise ExportFailure("E-006", f"local recording missing: {p}", item_id=it["item_id"])
        self.io.makedirs(dest.parent)
        self.io.discard_partial(dest)
        duration = sum(float(s["duration_s"] or 0) for s in segs)
        try:
            remux_concat(self.cfg.tools.ffmpeg, paths, partial_path(dest),
                         self.tmp_dir / f"{self.batch_id}-{it['item_id']}.ffconcat", timeout=600 + duration,
                         popen_kwargs=self.platform.low_priority_popen_kwargs())
            size = self.io.finish_external(dest)
        except (OSError, MediaError) as exc:
            f = self._classify(exc)
            try:
                self.io.discard_partial(dest)
            except OSError:
                pass
            if not f.interrupted:
                self.xl.set_item_state(it["item_id"], I_FAILED, error=f.detail[:500])
            raise f from None
        self.xl.set_item_state(it["item_id"], I_COPIED, size_bytes=size, copied_utc=iso_utc(time.time()))

    # ------------------------------------------------------------------ verification
    def _read_hash(self, dest: Path) -> tuple[str, int, str]:
        h = hashlib.sha256()
        n = 0
        chunks, method = self.io.read_back(dest)
        for c in chunks:
            h.update(c)
            n += len(c)
        return h.hexdigest(), n, method

    def _verify_item(self, it: Any) -> None:
        dest = self.root / it["dest_rel_path"]
        weight = int(it["planned_bytes"] or 0)
        if it["kind"] == "mp4":
            self._verify_mp4(it, dest, existing=False)
        else:
            try:
                sha, size, method = self._read_hash(dest)
            except OSError as exc:
                raise self._classify(exc) from None
            if sha != it["expected_sha256"] or (it["size_bytes"] is not None and size != it["size_bytes"]):
                self.xl.set_item_state(it["item_id"], I_FAILED, sha256=sha, verify_method=method,
                                       error="read-back SHA-256 differs from the source")
                self._event("error", "export_verify_failed", "A copied file did not verify",
                            {"path": it["dest_rel_path"], "expected": it["expected_sha256"], "read_back": sha})
                raise ExportFailure("E-003", f"read-back hash mismatch: {it['dest_rel_path']}", item_id=it["item_id"])
            self.io.seal_read_only(dest)
            self.xl.set_item_state(it["item_id"], I_VERIFIED, sha256=sha, size_bytes=size, verify_method=method,
                                   verified_utc=iso_utc(time.time()))
        self._progress(weight)

    def _existing(self, it: Any, dest: Path) -> None:
        """Destination already exists: never overwrite. Same content → verify and record; else conflict."""
        if it["kind"] == "mp4":
            self._verify_mp4(it, dest, existing=True)
            return
        try:
            sha, size, method = self._read_hash(dest)
        except OSError as exc:
            raise self._classify(exc) from None
        if sha != it["expected_sha256"]:
            self._conflict(it, sha)
        self.io.seal_read_only(dest)
        self.xl.set_item_state(it["item_id"], I_VERIFIED, sha256=sha, size_bytes=size, verify_method=method,
                               verified_utc=iso_utc(time.time()), already_present=1)
        self._event("info", "export_existing_verified", "File already on the drive with the same content; "
                    "re-verified, not copied again", {"path": it["dest_rel_path"]})

    def _conflict(self, it: Any, found_sha: str) -> None:
        self.xl.set_item_state(it["item_id"], I_FAILED, sha256=found_sha,
                               error="a different file with this name already exists on the drive; not overwritten")
        self._event("error", "export_conflict", "A different file with the same name is already on the drive; "
                    "nothing was overwritten", {"path": it["dest_rel_path"], "found_sha256": found_sha,
                                                "expected_sha256": it["expected_sha256"]})
        raise ExportFailure("E-004", f"existing different file: {it['dest_rel_path']}", item_id=it["item_id"])

    def _sources(self, it: Any) -> list[Any]:
        ids = json.loads(it["sources"])
        out = []
        for sid in ids:
            s = self.ledger.get_segment(sid)  # type: ignore[union-attr]
            if s is None:
                raise ExportFailure("E-006", f"source segment {sid} not in ledger", item_id=it["item_id"])
            out.append(s)
        return out

    def _expected_frames(self, it: Any) -> tuple[list[str], dict[str, Any]]:
        frames: list[str] = []
        computed: dict[str, Any] = {}
        from ..media.videohash import parse_framehash

        for s in self._sources(it):
            src = self.cfg.spool_path / s["rel_path"]
            seg_frames: list[str] | None = None
            if s["framehash_rel_path"] and s["framehash_sha256"]:
                fh = self.cfg.spool_path / s["framehash_rel_path"]
                try:
                    data = fh.read_bytes()
                except OSError as exc:
                    raise ExportFailure("E-006", f"per-frame hash file unreadable: {exc}", item_id=it["item_id"]) from None
                if hashlib.sha256(data).hexdigest() != s["framehash_sha256"]:
                    raise ExportFailure("E-006", f"per-frame hash file changed since sealing: {fh.name}",
                                        item_id=it["item_id"])
                seg_frames = [f.sha256 for f in parse_framehash(data.decode("ascii", "replace"))]
                if s["content_sha256"] and content_hash_of(seg_frames) != s["content_sha256"]:
                    raise ExportFailure("E-006", f"per-frame list does not match the content hash: {fh.name}",
                                        item_id=it["item_id"])
            if seg_frames is None:
                # No per-frame list yet (content hashing deferred / disabled): compute it on demand
                # from the sealed local original. Not written back to the recorder's ledger or spool.
                try:
                    seg_frames = frame_hashes_from_path(self.cfg.tools.ffmpeg, src, timeout=600 + 4 * float(
                        s["duration_s"] or 0), popen_kwargs=self.platform.low_priority_popen_kwargs())
                except MediaError as exc:
                    raise ExportFailure("E-006", f"could not decode local recording {src.name}: {exc} "
                                                 f"{exc.stderr_tail}", item_id=it["item_id"]) from None
                computed[s["segment_id"]] = {
                    "content_sha256": content_hash_of(seg_frames), "frame_count": len(seg_frames),
                    "method": f"{METHOD_PREFIX};pix_fmt={s['pix_fmt'] or 'decoder-native'};"
                              f"ffmpeg={short_version(self._ffmpeg_version)}", "computed_at_export": True}
            frames.extend(seg_frames)
        return frames, computed

    def _verify_mp4(self, it: Any, dest: Path, existing: bool) -> None:
        expected, computed = self._expected_frames(it)
        h = hashlib.sha256()
        size = [0]

        def on_chunk(c: bytes) -> None:
            h.update(c)
            size[0] += len(c)

        duration = sum(float(s["duration_s"] or 0) for s in self._sources(it))
        method = "unknown"
        try:
            chunks, method = self.io.read_back(dest)
            got = frame_hashes_from_stream(self.cfg.tools.ffmpeg, chunks, timeout=600 + 4 * duration,
                                           popen_kwargs=self.platform.low_priority_popen_kwargs(), on_chunk=on_chunk)
        except MediaError as exc:
            if not self.io.root_present(self.root):
                raise ExportFailure("E-002", str(exc), interrupted=True) from None
            got = None
            err = f"{exc} {exc.stderr_tail}"
        except OSError as exc:
            raise self._classify(exc) from None
        else:
            err = None
        sha = h.hexdigest()
        known = self.xl.known_sha_on_drive(self.drive_id or "", it["dest_rel_path"]) if existing else None
        reference = "per-segment-frame-lists"
        damaged: list[int] = []
        if got is not None and got != expected and len(got) == len(expected):
            # Damaged (truncated) frames are concealed differently depending on decoder state, so a
            # segment decoded alone can differ from the same packets decoded in the joined stream.
            # Re-derive the reference by decoding the ORIGINAL segments as one stream (local files,
            # no remux) and require the MP4 read back from the drive to match it exactly.
            try:
                joined = frame_hashes_from_concat(
                    self.cfg.tools.ffmpeg, [self.cfg.spool_path / s["rel_path"] for s in self._sources(it)],
                    self.tmp_dir / f"{self.batch_id}-{it['item_id']}-ref.ffconcat", timeout=600 + 4 * duration,
                    popen_kwargs=self.platform.low_priority_popen_kwargs())
            except MediaError as exc:
                raise ExportFailure("E-006", f"could not decode local recordings: {exc} {exc.stderr_tail}",
                                    item_id=it["item_id"]) from None
            if got == joined:
                damaged = [i for i, (a, b) in enumerate(zip(got, expected)) if a != b]
                reference = "joined-decode-of-originals"
                expected = joined
                log.warning("%s: %d damaged frame(s) verified against the joined decode of the originals",
                            it["dest_rel_path"], len(damaged))
        if got != expected or (known is not None and known != sha):
            diff = None
            if got is not None:
                diff = next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), min(len(got), len(expected)))
            detail = {"expected_frames": len(expected), "decoded_frames": None if got is None else len(got),
                      "first_difference_index": diff, "decode_error": err, "known_sha256": known}
            if existing:
                self._conflict(it, sha)
            self.xl.set_item_state(it["item_id"], I_FAILED, sha256=sha, verify_method=method, detail=detail,
                                   error="decoded frames of the MP4 read back from the drive differ from the originals")
            self._event("error", "export_verify_failed", "An MP4 video did not match its original recordings",
                        dict(detail, path=it["dest_rel_path"]))
            raise ExportFailure("E-003", f"MP4 content mismatch: {it['dest_rel_path']} {detail}", item_id=it["item_id"])
        if not existing and it["size_bytes"] is not None and size[0] != it["size_bytes"]:
            self.xl.set_item_state(it["item_id"], I_FAILED, sha256=sha, error="read-back size differs")
            raise ExportFailure("E-003", f"MP4 size mismatch: {it['dest_rel_path']}", item_id=it["item_id"])
        self.io.seal_read_only(dest)
        detail = json.loads(it["detail"] or "{}")
        detail["source_content"] = computed
        detail["content_reference"] = reference
        if damaged:
            detail["frames_differing_from_segment_lists"] = damaged
            detail["flags"] = sorted(set(detail.get("flags", [])) | {"damaged_frames"})
        cols: dict[str, Any] = dict(sha256=sha, size_bytes=size[0], verify_method=method,
                                    content_sha256=content_hash_of(got or []), frame_count=len(got or []),
                                    verified_utc=iso_utc(time.time()), detail=detail)
        if existing:
            cols["already_present"] = 1
        self.xl.set_item_state(it["item_id"], I_VERIFIED, **cols)

    # ------------------------------------------------------------------ manifest stage
    def _write_documents(self) -> str:
        bid = self.batch_id
        assert bid is not None
        b = self.xl.batch(bid)
        assert b is not None
        if b["manifest_generated_utc"] is None:
            self.xl.update_batch(bid, manifest_generated_utc=iso_utc(time.time()))
            b = self.xl.batch(bid)
        items = [dict(r) for r in self.xl.items(bid)]
        seg_ids = {i["segment_id"] for i in items if i["segment_id"]}
        for i in items:
            if i["kind"] == "mp4":
                seg_ids.update(json.loads(i["sources"] or "[]"))
        segments = {sid: dict(self.ledger.get_segment(sid)) for sid in seg_ids}  # type: ignore[union-attr]
        drive_row = self.ledger.query("SELECT identity FROM drives WHERE drive_id=?", (b["drive_id"],))  # type: ignore[union-attr]
        drive = json.loads(drive_row[0]["identity"]) if drive_row else {}
        mb = ManifestBuilder(dict(b), items, segments, drive, self.tz)
        mdir = layout.manifest_dir(b["stamp"], bid)
        docs: list[dict[str, Any]] = []
        docs.append(self._document(layout.README, mb.readme(), keep_existing=True))
        docs.append(self._document(layout.summary_rel_path(b["stamp"], bid), mb.html()))
        docs.append(self._document(f"{mdir}/Export_Manifest.csv", mb.csv_bytes()))
        manifest = self._document(f"{mdir}/Export_Manifest.json", mb.manifest(list(docs)))
        docs.append(manifest)
        sums = self._document(f"{mdir}/SHA256SUMS.txt", mb.sums(docs))
        self.xl.update_batch(bid, manifest_rel_path=manifest["path"], manifest_sha256=manifest["sha256"],
                             sums_sha256=sums["sha256"])
        return manifest["path"]

    def _document(self, rel: str, data: bytes, keep_existing: bool = False) -> dict[str, Any]:
        bid = self.batch_id
        assert bid is not None
        sha = sha256_bytes(data)
        item_id = self.xl.add_item(bid, NewItem("document", rel, expected_sha256=sha, planned_bytes=len(data)))
        it = self.xl.item(item_id)
        dest = self.root / rel
        if it["state"] == I_VERIFIED:
            return {"path": rel, "sha256": it["sha256"], "size_bytes": it["size_bytes"]}
        if it["expected_sha256"] != sha:
            if it["state"] == I_PLANNED and not self.io.exists(dest):
                self.xl.update_item(item_id, expected_sha256=sha)
            else:
                raise ExportFailure("E-007", f"regenerated document differs from the first attempt: {rel}")
        try:
            if self.io.exists(dest) and it["state"] == I_PLANNED:
                fsha, fsize, method = self._read_hash(dest)
                if fsha != sha and not keep_existing:
                    self._conflict(it, fsha)
                self.xl.set_item_state(item_id, I_VERIFIED, sha256=fsha, size_bytes=fsize, verify_method=method,
                                       verified_utc=iso_utc(time.time()), already_present=1,
                                       detail={"kept_existing_different_text": fsha != sha} if fsha != sha else None)
                return {"path": rel, "sha256": fsha, "size_bytes": fsize}
            if it["state"] == I_PLANNED:
                self.io.write_new(dest, [data], expected_sha256=sha)
                self.xl.set_item_state(item_id, I_COPIED, size_bytes=len(data), copied_utc=iso_utc(time.time()))
            fsha, fsize, method = self._read_hash(dest)
        except (OSError, MediaError) as exc:
            raise self._classify(exc) from None
        if fsha != sha:
            self.xl.set_item_state(item_id, I_FAILED, sha256=fsha, verify_method=method, error="read-back mismatch")
            raise ExportFailure("E-003", f"document read-back mismatch: {rel}", item_id=item_id)
        self.io.seal_read_only(dest)
        self.xl.set_item_state(item_id, I_VERIFIED, sha256=fsha, size_bytes=fsize, verify_method=method,
                               verified_utc=iso_utc(time.time()))
        return {"path": rel, "sha256": fsha, "size_bytes": fsize}

    # ------------------------------------------------------------------ outcome
    def _fail(self, f: ExportFailure) -> ExportResult:
        bid = self.batch_id
        assert bid is not None
        alert = make_alert(f.code, Severity.CRITICAL)
        state = B_INTERRUPTED if f.interrupted else B_FAILED
        log.error("export %s %s: %s", bid, state, f.detail)
        try:
            self.xl.set_batch_state(bid, state, error_code=f.code, error_detail=f.detail[:2000])
        except Exception:
            log.exception("could not record export failure in the ledger")
        if not f.interrupted and self.io.root_present(self.root):
            for it in self.xl.items(bid):  # free space taken by unfinished copies; never touch final files
                if it["state"] != I_VERIFIED:
                    try:
                        self.io.discard_partial(self.root / it["dest_rel_path"])
                    except OSError:
                        pass
        self._event("error" if not f.interrupted else "warning",
                    "export_interrupted" if f.interrupted else "export_failed", alert.message,
                    {"code": f.code, "detail": f.detail[:1000], "item_id": f.item_id})
        self._status("INTERRUPTED" if f.interrupted else "FAILED", alert.message, alert=alert.to_dict())
        return ExportResult(state, bid, alert.message, code=f.code, detail=f.detail)

    def _fail_without_batch(self, f: ExportFailure) -> ExportResult:
        alert = make_alert(f.code, Severity.CRITICAL)
        log.error("export not started: %s", f.detail)
        try:
            self._event("error", "export_not_started", alert.message, {"code": f.code, "detail": f.detail[:1000]})
        except Exception:
            log.exception("could not record event")
        self._status("FAILED", alert.message, alert=alert.to_dict())
        return ExportResult("FAILED", None, alert.message, code=f.code, detail=f.detail)

    # ------------------------------------------------------------------ status / events
    def _event(self, severity: str, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        d = dict(detail or {}, batch_id=self.batch_id, drive_id=self.drive_id)
        self.ledger.add_event(severity, "export", code, message, d)  # type: ignore[union-attr]

    def _percent(self) -> int:
        if not self.bytes_total:
            return 0
        return min(98, int(100 * self.bytes_done / self.bytes_total))

    def _progress(self, n: int) -> None:
        self.bytes_done += n
        if self._phase == "COPYING":
            msg = f"Copying recordings to the review drive ({self._percent()}%). Do not remove the drive."
        else:
            msg = f"Checking the copied files ({self._percent()}%). Do not remove the drive."
        self._status(self._phase, msg, percent=self._percent(), force=False)

    def _status(self, state: str, message: str, percent: int | None = None, force: bool = True,
                alert: dict[str, Any] | None = None) -> None:
        self.status.write(state, message, percent=percent, force=force, alert=alert, batch_id=self.batch_id,
                          camera_id=self.camera_id, drive_label=self.target.label,
                          bytes_done=self.bytes_done, bytes_total=self.bytes_total)


def export_once(cfg: AppConfig, target: ExportTarget, **kwargs: Any) -> ExportResult:
    return ReviewExporter(cfg, target, **kwargs).run()


