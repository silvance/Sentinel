"""Review-drive export end to end: real recordings (RecorderService + SimulatedCamera + FFmpeg),
temporary directories as drives, fault-injecting target I/O for full / removed drives.

Docs: docs/04 §6. Requirement IDs: U3, U4, S5, E2, E4, E7.
"""

from __future__ import annotations

import csv
import errno
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import base_config_dict, make_config, requires_ffmpeg
from harness import Harness, wait_for
from sentinel.clock import add_seconds_iso, iso_utc, parse_iso_utc
from sentinel.config import AppConfig
from sentinel.db import InternalGap, Ledger
from sentinel.export.engine import ExportResult, ReviewExporter
from sentinel.export.fileio import M_WIN_UNBUFFERED, PARTIAL, TargetIO
from sentinel.export.target import ExportTarget, RemovalResult
from sentinel.hashing import sha256_file
from sentinel.media.probe import ffmpeg_version, probe_segment
from sentinel.recorder.integrity import IntegrityWorker
from sentinel.models import SegmentState
from sentinel.platform import get_platform
from sentinel.sim.camera import SimulatedCamera

pytestmark = [pytest.mark.integration, requires_ffmpeg]

TZ = {"display_timezone": "America/New_York"}
SRC = Path(__file__).resolve().parents[2] / "src"


# ------------------------------------------------------------------ recordings
def _record(tmp: Path, before: int, after: int, **recording: Any) -> None:
    """Record ``before`` segments, drop the camera connection (=> gap + new run), record more."""
    cam = SimulatedCamera(fps=15).start()
    h = Harness(tmp, cam.url, recording=dict(TZ, **recording))
    h.start()
    try:
        h.wait_sealed(before, timeout=60)
        cam.drop_clients()
        wait_for(lambda: len(h.runs()) >= 2, 30, what="second run")
        h.wait_sealed(before + after + 1, timeout=60)
    finally:
        h.stop()
        cam.stop()
    if recording.get("content_hash", "deferred") == "deferred":
        # Segments sealed just before shutdown may still lack their deferred per-frame list:
        # finish it with the recorder's own integrity worker (as the service would on restart).
        led = Ledger(h.cfg.data_path / "sentinel.db")
        try:
            w = IntegrityWorker(h.cfg, led, get_platform(), ffmpeg_version(h.cfg.tools.ffmpeg))
            for s in led.segments_needing_content_hash():
                w.hash_segment(s["segment_id"])
        finally:
            led.close()


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("rec")
    _record(d, 3, 2)
    return d


class Env:
    def __init__(self, tmp: Path, **recording: Any) -> None:
        self.tmp = tmp
        self.recording = dict(TZ, **recording)
        self.cfg: AppConfig = make_config(tmp, "tcp://127.0.0.1:9", recording=self.recording)
        self.ledger = Ledger(self.cfg.data_path / "sentinel.db")

    def set_export(self, **export: Any) -> None:
        self.cfg = make_config(self.tmp, "tcp://127.0.0.1:9", recording=self.recording, export=export)

    def verify_content(self, target: ExportTarget) -> ExportResult:
        return ReviewExporter(self.cfg, target, ledger=self.ledger, allow_same_volume=True).verify_content()

    def close(self) -> None:
        self.ledger.close()

    def q(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.ledger.query(sql, params)]

    def drive(self, name: str) -> ExportTarget:
        root = self.tmp / f"drive_{name}"
        root.mkdir(exist_ok=True)
        return ExportTarget(root, {"drive_uuid": f"test-{name}", "label": f"REVIEW_{name}"})

    def export(self, target: ExportTarget, io: TargetIO | None = None, **kw: Any) -> ExportResult:
        return ReviewExporter(self.cfg, target, ledger=self.ledger, io=io, allow_same_volume=True,
                              operator_id=kw.pop("operator_id", "op-17"), **kw).run()

    def sealed(self, states: Iterable[str] = ("FINALIZED", "INTERRUPTED")) -> list[dict[str, Any]]:
        st = list(states)
        return self.q(f"SELECT * FROM segments WHERE state IN ({','.join('?' for _ in st)}) ORDER BY start_utc", tuple(st))

    def items(self, batch_id: str) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM export_items WHERE batch_id=? ORDER BY ordinal", (batch_id,))

    def batch(self, batch_id: str) -> dict[str, Any]:
        return self.q("SELECT * FROM export_batches WHERE batch_id=?", (batch_id,))[0]

    def status(self) -> dict[str, Any]:
        return json.loads((self.cfg.data_path / "export_status.json").read_text("utf-8"))


@pytest.fixture()
def env(recorded: Path, tmp_path: Path) -> Iterator[Env]:
    shutil.copytree(recorded, tmp_path, dirs_exist_ok=True)
    e = Env(tmp_path)
    yield e
    e.close()


# ------------------------------------------------------------------ fault-injecting drives
class FaultyIO(TargetIO):
    """Simulates a drive that fills up or is unplugged at a chosen point."""

    def __init__(self, *, free: int | None = None, quota: int | None = None, unplug_on_write_file: int | None = None,
                 unplug_on_read_file: int | None = None) -> None:
        self.free = free
        self.quota = quota
        self.written = 0
        self.unplug_on_write_file = unplug_on_write_file
        self.unplug_on_read_file = unplug_on_read_file
        self.write_files = 0
        self.read_files = 0
        self.unplugged = False
        self.writes: list[str] = []

    def _gone(self) -> None:
        if self.unplugged:
            raise OSError(errno.ENODEV, "The device is not connected")

    def free_bytes(self, root: Path) -> int:
        return self.free if self.free is not None else super().free_bytes(root)

    def root_present(self, root: Path) -> bool:
        return not self.unplugged and super().root_present(root)

    def exists(self, path: Path) -> bool:
        self._gone()
        return super().exists(path)

    def discard_partial(self, dest: Path) -> bool:
        self._gone()               # an unplugged drive cannot be tidied: the .partial stays
        return super().discard_partial(dest)

    def write_new(self, dest: Path, chunks: Iterable[bytes], expected_sha256: str | None = None) -> tuple[str, int]:
        self._gone()
        self.write_files += 1
        self.writes.append(dest.as_posix())
        return super().write_new(dest, chunks, expected_sha256)

    def _write(self, fh: object, data: bytes) -> None:
        self._gone()
        if self.quota is not None and self.written + len(data) > self.quota:
            raise OSError(errno.ENOSPC, "No space left on device")
        if self.unplug_on_write_file is not None and self.write_files == self.unplug_on_write_file:
            half = data[: len(data) // 2]
            fh.write(half)  # type: ignore[attr-defined]
            fh.flush()      # type: ignore[attr-defined]
            self.unplugged = True
            raise OSError(errno.EIO, "The device is not connected")
        self.written += len(data)
        super()._write(fh, data)

    def check_external(self, path: Path) -> None:
        self._gone()
        size = path.stat().st_size
        if self.quota is not None and self.written + size > self.quota:
            raise OSError(errno.ENOSPC, "No space left on device")
        self.written += size

    def read_back(self, path: Path) -> tuple[Iterator[bytes], str]:
        self._gone()
        self.read_files += 1
        chunks, method = super().read_back(path)
        if self.unplug_on_read_file is not None and self.read_files == self.unplug_on_read_file:
            def gen() -> Iterator[bytes]:
                for i, c in enumerate(chunks):
                    if i == 0:
                        yield c
                    self.unplugged = True
                    raise OSError(errno.EIO, "The device is not connected")
            return gen(), method
        return chunks, method

    def finish_external(self, dest: Path) -> int:
        self._gone()
        return super().finish_external(dest)


class CorruptingIO(TargetIO):
    """Flips bytes in the middle of files as they land on the drive (a bad cable / bad sector)."""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def finish_external(self, dest: Path) -> int:
        if self.kind == "mp4":
            from sentinel.export.fileio import partial_path
            p = partial_path(dest)
            data = bytearray(p.read_bytes())
            mid = len(data) * 2 // 3
            for i in range(mid, mid + 4000):
                data[i] ^= 0x5A
            p.write_bytes(bytes(data))
        return super().finish_external(dest)

    def read_back(self, path: Path) -> tuple[Iterator[bytes], str]:
        chunks, method = super().read_back(path)
        if self.kind == "original" and path.suffix == ".ts":
            def gen() -> Iterator[bytes]:
                for i, c in enumerate(chunks):
                    yield (bytes([c[0] ^ 1]) + c[1:]) if i == 0 and c else c
            return gen(), method
        return chunks, method


class FakeEjector:
    def __init__(self) -> None:
        self.flushed = self.ejected = 0

    def flush(self, target: ExportTarget) -> None:
        self.flushed += 1

    def eject(self, target: ExportTarget) -> RemovalResult:
        self.ejected += 1
        return RemovalResult(True, "SAFE TO REMOVE DRIVE", "fake")


# ------------------------------------------------------------------ checks
def _top_level_boxes(p: Path) -> list[str]:
    out = []
    with open(p, "rb") as fh:
        while True:
            hdr = fh.read(8)
            if len(hdr) < 8:
                return out
            size, typ = struct.unpack(">I4s", hdr)
            out.append(typ.decode("latin-1"))
            if size == 1:
                size = struct.unpack(">Q", fh.read(8))[0]
                fh.seek(size - 16, 1)
            elif size == 0:
                return out
            else:
                fh.seek(size - 8, 1)


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _check_drive(env: Env, target: ExportTarget, res: ExportResult) -> dict[str, Any]:
    """Every invariant a COMPLETE review drive must satisfy. Returns the parsed manifest."""
    root = target.root
    assert res.state == "COMPLETE", res
    b = env.batch(res.batch_id)  # type: ignore[arg-type]
    items = env.items(res.batch_id)  # type: ignore[arg-type]
    assert b["state"] == "COMPLETE" and all(i["state"] == "VERIFIED" for i in items)
    assert not [p for p in _files(root) if p.name.endswith(PARTIAL)], "no .partial left behind"
    if sys.platform == "win32":
        assert {i["verify_method"] for i in items} == {M_WIN_UNBUFFERED}

    mpath = root / b["manifest_rel_path"]
    assert sha256_file(mpath)[0] == b["manifest_sha256"]
    man = json.loads(mpath.read_text("utf-8"))
    assert man["export_id"] == b["batch_id"] and man["operator_id"] == "op-17"
    assert man["interval"]["display_timezone"] == "America/New_York"
    assert man["drive"]["drive_id"] == b["drive_id"] == target.drive_key

    # SHA256SUMS: every listed file re-hashed independently matches; lists every evidence file.
    sums = (mpath.parent / "SHA256SUMS.txt").read_text("utf-8").splitlines()
    listed = {}
    for line in sums:
        h, rel = line.split("  ", 1)
        listed[rel] = h
        assert hashlib.sha256((root / rel).read_bytes()).hexdigest() == h, rel
    assert {i["dest_rel_path"] for i in items if i["dest_rel_path"] != "Manifests/SHA256SUMS.txt"} - set(listed) <= {
        i["dest_rel_path"] for i in items if i["dest_rel_path"].endswith("SHA256SUMS.txt")}
    assert {f["path"] for f in man["files"]} <= set(listed)

    # CSV has one row per evidence file
    mcsv = list(csv.DictReader((mpath.parent / "Export_Manifest.csv").read_text("utf-8-sig").splitlines()))
    assert len(mcsv) == len(man["files"])

    # Originals: byte-identical to the sealed local originals; local spool untouched.
    for o in man["originals"]:
        seg = env.q("SELECT * FROM segments WHERE segment_id=?", (o["segment_id"],))[0]
        assert o["sealed_sha256"] == seg["sha256"]
        assert sha256_file(root / o["exported_path"])[0] == seg["sha256"]
        local = env.cfg.spool_path / seg["rel_path"]
        assert sha256_file(local)[0] == seg["sha256"] and get_platform().is_read_only(local)

    # Derivatives: play (ffprobe), faststart, never claim the original's hash, frames verified.
    source_hashes = {s["sha256"] for s in env.sealed(("FINALIZED", "INTERRUPTED", "CORRUPT"))}
    for d in man["derivatives"]:
        p = root / d["path"]
        assert d["path"].startswith("Recordings/") and d["path"].endswith(".mp4")
        assert "_EST_UTC-0500" in d["path"] or "_EDT_UTC-0400" in d["path"]
        r = probe_segment(p)
        assert r.playable and r.codec == "h264", r.detail()
        boxes = _top_level_boxes(p)
        assert boxes.index("moov") < boxes.index("mdat"), boxes
        assert d["sha256"] not in source_hashes and d["sha256"] == sha256_file(p)[0]
        srcs = [env.q("SELECT * FROM segments WHERE segment_id=?", (s["segment_id"],))[0] for s in d["sources"]]
        assert len({s["run_id"] for s in srcs}) == 1, "an MP4 never spans two recording runs"
        assert r.duration_s == pytest.approx(sum(s["duration_s"] for s in srcs), abs=0.5)
        cc = d["content_check"]
        assert cc["level"] in ("full", "sampled", "deferred") and cc["description"]
        assert d["written_sha256"] == d["sha256"], "byte verification: as written == read back"
        if cc["level"] == "full":
            assert d["frame_count"] > 0 and d["content_sha256"] and cc["spans"] == [[0, d["frame_count"]]]
        elif cc["level"] == "sampled":
            assert d["frame_count"] > 0 and d["content_sha256"] is None and cc["frames_checked"] > 0
            assert cc["spans"][0][0] == 0 and cc["spans"][-1][1] == d["frame_count"]
        else:
            assert cc["reason"] and d["content_sha256"] is None
    html = (root / [x["path"] for x in man["documents"] if x["path"].startswith("Reports/")][0]).read_text("utf-8")
    assert "http://" not in html and "https://" not in html and "<script" not in html and "src=" not in html
    assert (root / "README_FIRST.txt").read_text("utf-8").count("Get-FileHash") >= 1
    return man


# ================================================================== tests
def test_happy_path_review_drive(env: Env) -> None:
    t = env.drive("A")
    res = env.export(t)
    man = _check_drive(env, t, res)
    sealed = env.sealed()
    assert {o["segment_id"] for o in man["originals"]} == {s["segment_id"] for s in sealed}
    # the camera drop split the footage: one file per run, and the gap is listed and highlighted
    assert len(man["derivatives"]) == len({s["run_id"] for s in sealed}) == 2
    gap = [g for g in man["gaps"] if g["kind"] == "between_runs" and not g["open"]]
    assert gap and gap[0]["start_utc"] >= man["derivatives"][0]["end_utc"]
    assert gap[0]["end_utc"] <= man["derivatives"][1]["start_utc"]
    assert man["coverage"]["unaccounted"] == []
    assert gap[0]["gap_id"] in man["derivatives"][0]["gap_ids_after"]
    # Self-describing for an offline tool: every media file carries ids, hashes, times and format.
    assert man["format"] == "sentinel-review-export" and man["format_version"] == 1
    for f in man["files"]:
        if f["kind"] in ("original", "mp4"):
            assert f["segment_ids"] and f["sha256"] and f["start_utc"] and f["end_utc"] and f["start_local"]
            assert f["video_codec"] == "h264" and f["width"] == 640 and f["height"] == 480
        if f["kind"] == "mp4":
            assert f["content_check"] == "sampled"          # the configured (provisional) default
    assert all(d["content_check"]["level"] == "sampled" for d in man["derivatives"])
    assert man["verification"]["content_check_configured"] == "sampled"
    assert "sample of frames checked" in next(p for p in (t.root / "Reports").iterdir()).read_text("utf-8")
    with_sidecar = [f for f in man["files"] if f["kind"] == "original" and f["framehash_path"]]
    assert with_sidecar and all((t.root / f["framehash_path"]).exists() for f in with_sidecar)
    drive_row = env.q("SELECT * FROM drives WHERE drive_id=?", (man["drive"]["drive_id"],))[0]
    assert drive_row["drive_uuid"] == "test-A" and drive_row["label"] == "REVIEW_A"
    assert any(g["open"] and g["reason"] == "graceful_stop" for g in man["gaps"])
    html = next(p for p in (t.root / "Reports").iterdir()).read_text("utf-8")
    assert "class='gap'" in html
    st = env.status()
    assert st["state"] == "COMPLETE_REMOVE_MANUALLY" and st["percent"] == 100 and st["final"]
    assert "Safely Remove Hardware" in st["message"]
    assert env.q("SELECT * FROM events WHERE code='export_complete'")
    # Nothing new: a second press writes nothing.
    before = [(p, p.stat().st_size) for p in _files(t.root)]
    res2 = env.export(t)
    assert res2.state == "NOTHING_TO_EXPORT"
    assert [(p, p.stat().st_size) for p in _files(t.root)] == before


def test_ejector_is_called_and_safe_to_remove_shown(env: Env) -> None:
    ej = FakeEjector()
    res = env.export(env.drive("E"), remover=ej)
    assert res.state == "COMPLETE" and res.ejected and ej.flushed == 1 and ej.ejected == 1
    assert env.status()["state"] == "SAFE_TO_REMOVE"
    assert env.batch(res.batch_id)["ejected"] == 1  # type: ignore[arg-type]


def test_target_on_recording_storage_is_refused(env: Env) -> None:
    t = env.drive("same")
    res = ReviewExporter(env.cfg, t, ledger=env.ledger).run()      # allow_same_volume defaults to False
    assert res.state == "FAILED" and res.code == "E-005" and "same volume" in (res.detail or "")
    for name in ("recording storage", "recorder database", "recorder logs", "system disk"):
        assert name in (res.detail or "")       # temp dirs share the system volume on this host
    assert _files(t.root) == []


def test_volume_check_fails_closed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import sentinel.export.target as tgt

    t = env.drive("closed")
    real = tgt.os.stat

    def flaky(p: Any, *a: Any, **k: Any) -> Any:
        if Path(p) == t.root:
            raise PermissionError(13, "Access is denied")
        return real(p, *a, **k)

    monkeypatch.setattr(tgt.os, "stat", flaky)
    res = ReviewExporter(env.cfg, t, ledger=env.ledger).run()
    assert res.state == "FAILED" and res.code == "E-005" and "cannot confirm" in (res.detail or "")


def test_drive_full_before_start(env: Env) -> None:
    t = env.drive("small")
    res = env.export(t, io=FaultyIO(free=10 * 1024 * 1024))
    assert res.state == "FAILED" and res.code == "E-001" and res.batch_id is None
    st = env.status()
    assert st["state"] == "FAILED" and st["alert"]["code"] == "E-001"
    assert st["message"].startswith("Error E-001: The review drive is full.")
    assert _files(t.root) == []


def test_drive_too_small_ends_early_and_next_drive_continues(env: Env) -> None:
    from zoneinfo import ZoneInfo

    from sentinel.export.planner import Planner
    plan = Planner(env.ledger, env.cfg.spool_path, "CAM01", ZoneInfo(TZ["display_timezone"])).plan("x", None, 10**12)
    assert plan is not None
    first_mp4 = next(n for n, i in enumerate(plan.items) if i.kind == "mp4")
    cost1 = sum(i.planned_bytes for i in plan.items[: first_mp4 + 1])
    free = 8 * 1024 * 1024 + 64 * 1024 * 1024 + cost1 + 50_000   # reserve + margin + first file only
    a, b = env.drive("A"), env.drive("B")
    r1 = env.export(a, io=FaultyIO(free=free))
    m1 = _check_drive(env, a, r1)
    assert len(m1["derivatives"]) == 1 and any(w["code"] == "drive_too_small" for w in m1["warnings"])
    r2 = env.export(b)
    m2 = _check_drive(env, b, r2)
    assert m2["interval"]["start_utc"] == m1["interval"]["end_utc"], "no hole between exports"
    ids1 = {o["segment_id"] for o in m1["originals"]}
    ids2 = {o["segment_id"] for o in m2["originals"]}
    assert not ids1 & ids2 and ids1 | ids2 == {s["segment_id"] for s in env.sealed()}


def test_disk_full_during_copy_fails_cleanly_then_same_drive_finishes(env: Env) -> None:
    t = env.drive("Q")
    res = env.export(t, io=FaultyIO(quota=700_000))
    assert res.state == "FAILED" and res.code == "E-001"
    b = env.batch(res.batch_id)  # type: ignore[arg-type]
    assert b["state"] == "FAILED" and b["error_code"] == "E-001"
    items = env.items(res.batch_id)  # type: ignore[arg-type]
    assert not [i for i in items if i["state"] == "VERIFIED"], "nothing claimed verified"
    assert [i for i in items if i["state"] == "FAILED"]
    assert not [p for p in _files(t.root) if p.name.endswith(PARTIAL)], "partials removed to free space"
    copied = [p.as_posix() for p in _files(t.root)]
    assert copied
    assert env.status()["alert"]["code"] == "E-001"
    # The operator frees space (here: lifts the quota) and presses the button again: the FAILED
    # batch is retried on the same drive; files already copied are verified, not copied again.
    io2 = FaultyIO()
    res2 = env.export(t, io=io2)
    _check_drive(env, t, res2)
    assert res2.batch_id == res.batch_id and env.batch(res.batch_id)["attempts"] == 2  # type: ignore[arg-type]
    assert not set(copied) & set(io2.writes)


def test_drive_removed_mid_copy_then_resume(env: Env) -> None:
    t = env.drive("R")
    io1 = FaultyIO(unplug_on_write_file=3)
    res = env.export(t, io=io1)
    assert res.state == "INTERRUPTED" and res.code == "E-002"
    assert env.status()["state"] == "INTERRUPTED"
    assert "Insert the same drive again" in env.status()["message"]
    b = env.batch(res.batch_id)  # type: ignore[arg-type]
    assert b["state"] == "INTERRUPTED"
    partials = [p for p in _files(t.root) if p.name.endswith(PARTIAL)]
    assert len(partials) == 1, "the unplugged write left a .partial (never claimed)"
    first_two = io1.writes[:2]
    # Meanwhile an administrator changes the display time zone: the batch keeps its own.
    env.recording["display_timezone"] = "UTC"
    env.set_export()
    # Reinsert the same drive: the batch resumes, the .partial is discarded, finished files are
    # not copied again.
    io2 = FaultyIO()
    res2 = env.export(t, io=io2)
    assert res2.batch_id == res.batch_id
    _check_drive(env, t, res2)
    assert not set(first_two) & set(io2.writes)
    assert env.batch(res.batch_id)["attempts"] == 2  # type: ignore[arg-type]
    assert env.q("SELECT * FROM events WHERE code='export_resumed'")


def test_drive_removed_mid_verify_then_resume(env: Env) -> None:
    t = env.drive("V")
    io1 = FaultyIO(unplug_on_read_file=2)
    res = env.export(t, io=io1)
    assert res.state == "INTERRUPTED" and res.code == "E-002"
    items = env.items(res.batch_id)  # type: ignore[arg-type]
    states = [i["state"] for i in items if i["kind"] != "document"]
    assert states.count("VERIFIED") == 1 and states.count("COPIED") == len(states) - 1
    io2 = FaultyIO()
    res2 = env.export(t, io=io2)
    assert res2.batch_id == res.batch_id
    _check_drive(env, t, res2)
    # nothing re-copied: only the manifest documents were written on resume
    assert all(n.endswith((".txt", ".html", ".csv", ".json")) and "/Originals/" not in n for n in io2.writes), io2.writes


def test_existing_different_file_is_a_conflict_and_never_overwritten(env: Env) -> None:
    t = env.drive("C")
    seg = env.sealed()[1]
    dest = t.root / "Originals" / seg["rel_path"]
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"someone else's file")
    res = env.export(t)
    assert res.state == "FAILED" and res.code == "E-004"
    assert dest.read_bytes() == b"someone else's file"
    assert env.q("SELECT * FROM events WHERE code='export_conflict'")
    assert env.status()["alert"]["code"] == "E-004"


def test_existing_identical_file_is_reverified_not_copied(env: Env) -> None:
    t = env.drive("S")
    seg = env.sealed()[0]
    dest = t.root / "Originals" / seg["rel_path"]
    dest.parent.mkdir(parents=True)
    shutil.copyfile(env.cfg.spool_path / seg["rel_path"], dest)
    io = FaultyIO()
    res = env.export(t, io=io)
    _check_drive(env, t, res)
    it = [i for i in env.items(res.batch_id) if i["dest_rel_path"].endswith(seg["rel_path"])][0]  # type: ignore[arg-type]
    assert it["already_present"] == 1 and it["state"] == "VERIFIED"
    assert dest.as_posix() not in io.writes


def test_corrupt_segment_goes_to_quarantine(env: Env) -> None:
    gap = env.q("SELECT * FROM recording_gaps WHERE kind='between_runs' AND end_utc IS NOT NULL")[0]
    run = env.q("SELECT run_id, directory FROM recording_runs ORDER BY rowid")[0]
    rel = f"{run['directory']}/seg_000900.ts"
    p = env.cfg.spool_path / rel
    p.write_bytes(b"\0" * 50_000)
    sha, size = sha256_file(p)
    get_platform().seal_read_only(p)
    sid = env.ledger.insert_segment(camera_id="CAM01", run_id=run["run_id"], seq=900, rel_path=rel)
    start, end = gap["start_utc"], add_seconds_iso(gap["start_utc"], 0.5)
    env.ledger.seal(sid, SegmentState.CORRUPT,
                    dict(sha256=sha, size_bytes=size, start_utc=start, end_utc=end, duration_s=0.5,
                         sealed_utc=iso_utc(time.time()), warnings="[]"),
                    [InternalGap(start, end, 0.5, "corrupt_segment")], None, [])
    t = env.drive("Z")
    res = env.export(t)
    man = _check_drive(env, t, res)
    q = t.root / "Quarantine" / rel
    assert q.exists() and sha256_file(q)[0] == sha
    entry = [o for o in man["originals"] if o["segment_id"] == sid][0]
    assert entry["quarantined"] and entry["state"] == "CORRUPT"
    assert all(sid not in [s["segment_id"] for s in d["sources"]] for d in man["derivatives"])
    assert not (t.root / "Originals" / rel).exists()
    html = next((t.root / "Reports").iterdir()).read_text("utf-8")
    assert "Quarantine" in html and "Damaged" in html


def test_damaged_copy_is_detected(env: Env) -> None:
    res = env.export(env.drive("D1"), io=CorruptingIO("original"))
    assert res.state == "FAILED" and res.code == "E-003"
    assert env.q("SELECT * FROM events WHERE code='export_verify_failed'")
    res2 = env.export(env.drive("D2"), io=CorruptingIO("mp4"))
    assert res2.state == "FAILED" and res2.code == "E-003"
    bad = [i for i in env.items(res2.batch_id) if i["state"] == "FAILED"]  # type: ignore[arg-type]
    assert bad and bad[0]["kind"] == "mp4"
    assert env.status()["message"].startswith("Error E-003")


def _skew_lists(monkeypatch: pytest.MonkeyPatch, where: str, n: int = 1) -> None:
    """Make the per-segment lists of the LAST source of every MP4 differ from what the MP4 decodes
    to, as a damaged (cut-off) final frame can (measured 1 of 6 camera-drop recordings)."""
    orig = ReviewExporter._source_lists

    def skewed(self: ReviewExporter, it: Any, srcs: list[dict[str, Any]], compute_missing: bool) -> Any:
        lists, computed = orig(self, it, srcs, compute_missing)
        lists = [list(x) if x is not None else None for x in lists]
        if where == "tail":                      # last n frames of the MP4 (across sources if needed)
            left = n
            for src in reversed(lists):
                assert src is not None
                k = min(left, len(src))
                for i in range(len(src) - k, len(src)):
                    src[i] = "0" * 64
                left -= k
                if not left:
                    break
        else:                                    # one frame in the middle of the first source
            src = lists[0]
            assert src is not None and len(src) >= 8
            src[len(src) // 2] = "0" * 64
        return lists, computed

    monkeypatch.setattr(ReviewExporter, "_source_lists", skewed)


def _mark_last_segments_interrupted(env: Env) -> None:
    for run in env.q("SELECT run_id FROM recording_runs"):
        last = env.q("SELECT segment_id FROM segments WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run["run_id"],))[0]
        env.ledger.query("UPDATE segments SET state='INTERRUPTED', interrupted=1 WHERE segment_id=?",
                         (last["segment_id"],))


@pytest.mark.parametrize("level", ["full", "sampled"])
def test_damaged_final_frame_of_interrupted_segment_is_accepted_and_listed(env: Env, monkeypatch: pytest.MonkeyPatch,
                                                                         level: str) -> None:
    _mark_last_segments_interrupted(env)
    env.set_export(content_check=level)
    _skew_lists(monkeypatch, "tail", 1)
    t = env.drive("J")
    man = _check_drive(env, t, env.export(t))
    for d in man["derivatives"]:
        cc = d["content_check"]
        assert cc["level"] == level
        assert cc["unverifiable_damaged_frames"] == [d["frame_count"] - 1]
        assert "damaged_frames" in d["flags"]


@pytest.mark.parametrize("case", ["tail_of_intact_segment", "three_tail_frames", "middle_frame"])
def test_other_picture_differences_fail(env: Env, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    env.set_export(content_check="full")
    if case == "three_tail_frames":
        _mark_last_segments_interrupted(env)
        _skew_lists(monkeypatch, "tail", 3)
    elif case == "middle_frame":
        _mark_last_segments_interrupted(env)
        _skew_lists(monkeypatch, "middle", 1)
    else:  # intact (FINALIZED, decodes without errors): no difference allowed at all
        _skew_lists(monkeypatch, "tail", 1)
    res = env.export(env.drive("K"))
    assert res.state == "FAILED" and res.code == "E-003", res
    assert "pictures differ" in (res.detail or "")


def test_sampled_mode_detects_a_wrong_picture_at_a_sampled_position(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.set_export(content_check="sampled")
    _skew_lists(monkeypatch, "middle", 1)   # sources are only a few seconds: every frame is in a span
    res = env.export(env.drive("SP"))
    assert res.state == "FAILED" and res.code == "E-003"


def test_deferred_content_check_then_verify_content_completes_it(env: Env) -> None:
    env.set_export(content_check="deferred")
    t = env.drive("DF")
    man = _check_drive(env, t, env.export(t))
    assert all(d["content_check"]["level"] == "deferred" for d in man["derivatives"])
    assert "pictures not yet checked" in next((t.root / "Reports").iterdir()).read_text("utf-8")
    assert env.status()["state"] == "COMPLETE_REMOVE_MANUALLY"     # bytes verified => complete
    res = env.verify_content(t)
    assert res.state == "COMPLETE", res
    mp4s = env.q("SELECT * FROM export_items WHERE kind='mp4'")
    assert mp4s and all(i["content_check"] == "full" and i["content_sha256"] and i["frame_count"] for i in mp4s)
    reports = list(t.root.rglob("Content_Verification_*.json"))
    assert len(reports) == 1
    doc = json.loads(reports[0].read_text("utf-8"))
    assert [v["result"] for v in doc["videos"]] == ["OK"] * len(mp4s)
    assert env.q("SELECT * FROM export_items WHERE dest_rel_path LIKE '%Content_Verification_%' AND state='VERIFIED'")
    assert env.verify_content(t).state == "NOTHING_TO_EXPORT"


def test_full_mode_records_content_hash(env: Env) -> None:
    env.set_export(content_check="full")
    t = env.drive("FU")
    man = _check_drive(env, t, env.export(t))
    assert all(d["content_check"]["level"] == "full" and d["content_sha256"] for d in man["derivatives"])


def test_missing_frame_hashes_sampled_defers_full_computes_on_demand(tmp_path: Path) -> None:
    _record(tmp_path, 2, 1, content_hash="none", frame_hashes=False)
    e = Env(tmp_path, content_hash="none", frame_hashes=False)
    try:
        assert all(s["framehash_rel_path"] is None for s in e.sealed())
        t1 = e.drive("H1")
        m1 = _check_drive(e, t1, e.export(t1))           # default "sampled" cannot sample without lists
        assert all(d["content_check"]["level"] == "deferred" and "not yet available" in d["content_check"]["reason"]
                   for d in m1["derivatives"])
        e.set_export(content_check="full")
        t2 = e.drive("H2")
        e.ledger.query("DELETE FROM export_items")       # pretend H1 never happened: export the same footage
        e.ledger.query("DELETE FROM export_batches")
        man = _check_drive(e, t2, e.export(t2))
        assert man["originals"] and all(o["content_hash_computed_at_export"] for o in man["originals"])
        assert all(d["content_check"]["level"] == "full" for d in man["derivatives"])
        assert not list(t2.root.rglob("*.framehash.txt"))
        # the recorder's ledger is not modified by export
        assert all(s["content_sha256"] is None for s in e.sealed())
    finally:
        e.close()


def test_resume_rehashes_verified_files_and_detects_changes(env: Env) -> None:
    t = env.drive("RH")
    res = env.export(t, io=FaultyIO(unplug_on_read_file=3))
    assert res.state == "INTERRUPTED"
    verified = [i for i in env.items(res.batch_id) if i["state"] == "VERIFIED"]  # type: ignore[arg-type]
    assert len(verified) == 2
    victim = t.root / verified[0]["dest_rel_path"]
    os.chmod(victim, 0o666)
    data = bytearray(victim.read_bytes())
    data[100] ^= 0xFF                           # altered while the drive was elsewhere
    victim.write_bytes(bytes(data))
    res2 = env.export(t)
    assert res2.state == "FAILED" and res2.code == "E-003"
    assert env.q("SELECT * FROM events WHERE code='export_verified_file_changed'")


def test_resume_uses_segment_snapshot_not_live_rows(env: Env) -> None:
    t = env.drive("SN")
    res = env.export(t, io=FaultyIO(unplug_on_read_file=2))
    assert res.state == "INTERRUPTED"
    # The recorder changes ledger rows after planning (e.g. deferred hashing fills columns).
    env.ledger.query("UPDATE segments SET warnings='[\"changed_later\"]', content_hash_method='later'")
    man = _check_drive(env, t, env.export(t))
    # Integration: segments may legitimately carry warnings from sealing (e.g. the time-accuracy
    # branch's "time_correction_limited" under machine load); what matters is that the live-row
    # change made after planning does not leak into the manifest.
    assert all("changed_later" not in o["warnings"] and o["content_hash_method"] != "later" for o in man["originals"])


def test_export_survives_a_locked_ledger(env: Env) -> None:
    import sqlite3
    import threading

    env.ledger.query("PRAGMA busy_timeout=100")       # make the lock outlast SQLite's own wait
    other = sqlite3.connect(env.cfg.data_path / "sentinel.db", isolation_level=None, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")
    threading.Timer(2.0, lambda: other.execute("COMMIT")).start()
    try:
        res = env.export(env.drive("LK"))
    finally:
        time.sleep(2.1)
        other.close()
    assert res.state == "COMPLETE", res


def _toml(d: dict[str, Any]) -> str:
    out = []
    for sec, vals in d.items():
        out.append(f"[{sec}]")
        for k, v in vals.items():
            if isinstance(v, bool):
                out.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                out.append(f"{k} = {v}")
            else:
                out.append(f"{k} = {json.dumps(str(v))}")
        out.append("")
    return "\n".join(out)


def _cli_export(cfg_path: Path, target: Path) -> subprocess.CompletedProcess[str]:
    envv = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.run([sys.executable, "-m", "sentinel.export", "run", "--config", str(cfg_path), "--target",
                           str(target), "--operator-id", "op-17", "--allow-same-volume"],
                          capture_output=True, text=True, timeout=300, env=envv)


def test_export_runs_in_parallel_with_live_recording_and_second_export_has_no_hole(tmp_path: Path) -> None:
    cam = SimulatedCamera(fps=15).start()
    h = Harness(tmp_path, cam.url, recording=TZ)
    cfg_path = tmp_path / "sentinel.toml"
    cfg_path.write_text(_toml(base_config_dict(tmp_path, cam.url, recording=TZ)), "utf-8")
    a, b = tmp_path / "drive_A", tmp_path / "drive_B"
    a.mkdir()
    b.mkdir()
    h.start()
    try:
        h.wait_sealed(4, timeout=60)
        before = h.sealed_count()
        t0 = time.monotonic()
        cp1 = _cli_export(cfg_path, a)          # separate process, recorder keeps running
        assert cp1.returncode == 0, cp1.stderr[-2000:]
        assert "Export complete and verified" in cp1.stdout
        h.wait_sealed(h.sealed_count() + 3, timeout=60)
        during = h.sealed_count()
        cp2 = _cli_export(cfg_path, b)
        assert cp2.returncode == 0, cp2.stderr[-2000:]
        elapsed = time.monotonic() - t0
        h.wait_sealed(h.sealed_count() + 1, timeout=60)
        st = h.status()
        assert st["overall"] == "RECORDING", st
    finally:
        h.stop()
        cam.stop()
    assert during > before
    # recording was never disturbed: one run, no gap except the final graceful stop
    assert len(h.runs()) == 1, h.runs()
    assert [g["reason"] for g in h.gaps("between_runs")] == ["graceful_stop"]
    e = Env(tmp_path)
    try:
        batches = e.q("SELECT * FROM export_batches ORDER BY rowid")
        assert [x["state"] for x in batches] == ["COMPLETE", "COMPLETE"]
        assert batches[1]["interval_start_utc"] == batches[0]["interval_end_utc"], "no hole between exports"
        exported = [i["segment_id"] for i in e.q("SELECT segment_id FROM export_items WHERE kind='original'")]
        assert len(exported) == len(set(exported)), "no segment exported twice"
        covered = [s for s in e.sealed() if s["end_utc"] <= batches[1]["interval_end_utc"]]
        assert set(exported) == {s["segment_id"] for s in covered}
        for x, root in zip(batches, (a, b)):
            assert x["drive_id"].startswith(("volume_guid:", "fs_uuid:")) and "|subdir:" in x["drive_id"]
            ident = json.loads(e.q("SELECT identity FROM drives WHERE drive_id=?", (x["drive_id"],))[0]["identity"])
            _check_drive(e, ExportTarget(root, ident), ExportResult("COMPLETE", x["batch_id"], ""))
        status = e.status()
        assert status["state"] == "COMPLETE_REMOVE_MANUALLY"
    finally:
        e.close()
    print(f"two CLI exports during live recording took {elapsed:.1f}s")


def test_a_recording_that_cannot_be_sealed_does_not_stop_later_exports(env: Env) -> None:
    """Review 2026-11-12: a segment the finalizer gave up on (CLOSED, 5 attempts) capped every export at its
    start until a restart; later drives were told there were no new recordings."""
    segs = env.sealed()
    assert len(segs) >= 3
    stuck = segs[1]
    with env.ledger.tx() as c:
        c.execute("UPDATE segments SET state='CLOSED', finalize_attempts=5 WHERE segment_id=?", (stuck["segment_id"],))
    a = env.drive("A")
    r1 = env.export(a)
    m1 = _check_drive(env, a, r1)
    ids1 = {o["segment_id"] for o in m1["originals"]}
    later = {s["segment_id"] for s in segs[2:]}
    assert later <= ids1 and stuck["segment_id"] not in ids1          # exported past it
    w = next(x for x in m1["warnings"] if x["code"] == "footage_not_sealed")
    assert w["segment_ids"] == [stuck["segment_id"]]
    # Sealed later (a restart's recovery): it follows on the next drive as a late recording.
    with env.ledger.tx() as c:
        c.execute("UPDATE segments SET state=?, finalize_attempts=0 WHERE segment_id=?",
                  (stuck["state"], stuck["segment_id"]))
    b = env.drive("B")
    r2 = env.export(b)
    m2 = _check_drive(env, b, r2)
    assert stuck["segment_id"] in {o["segment_id"] for o in m2["originals"]}
