"""Archive drive, owner request 2026-11-11: two drives in a two-bay dock (copying moves on to the other
drive when one is full; Swap then takes out the full one), sidecars in Fingerprints/, and one playlist
per local day."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from archive_helpers import check_drive, claimed, drain, make_drive
from conftest import make_config
from sentinel.archive import index as idx
from sentinel.archive import layout
from sentinel.archive.io import ArchiveIO
from sentinel.archive.locator import DriveScan
from sentinel.archive.status import read_archive_status
from sentinel.archive.worker import ArchiveWorker
from sentinel.db import Ledger
from sentinel.export.target import ExportTarget
from tools.archive_bench import add_synthetic_run

SEG = 300_000


class CappedIO(ArchiveIO):
    """Each drive (temporary folder) holds at most ``caps[root]`` bytes; others are unlimited."""

    def __init__(self, caps: dict[Path, int]) -> None:
        super().__init__(read_chunk=1024 * 1024)
        self.caps = {Path(k): v for k, v in caps.items()}

    def free_bytes(self, root: Path) -> int:
        cap = self.caps.get(Path(root))
        if cap is None:
            return super().free_bytes(root)
        used = sum(p.stat().st_size for p in Path(root).rglob("*") if p.is_file())
        return max(0, cap - used)


class Bays:
    """A two-bay dock: whichever drives are in it."""

    def __init__(self, *targets: ExportTarget) -> None:
        self.targets = list(targets)

    def scan(self) -> DriveScan:
        return DriveScan([t for t in self.targets if Path(t.root).is_dir()])


class Env:
    def __init__(self, tmp: Path, **archive: Any) -> None:
        self.tmp = tmp
        a = {"enabled": True, "drive_path": str(tmp / "unused"), "require_drive_marker": False,
             "reserve_free_mb": 0.0, "poll_interval_s": 0.05, "chunk_mib": 1, "fsync_every_mib": 2,
             "max_rate_mb_s": 1000.0, "resume_policy": "any_drive"}
        a.update(archive)
        self.cfg = make_config(tmp, archive=a)
        self.ledger = Ledger(self.cfg.data_path / "sentinel.db")

    def worker(self, locator: Any, io: ArchiveIO) -> ArchiveWorker:
        return ArchiveWorker(self.cfg, ledger=self.ledger, locator=locator, io=io, allow_same_volume=True,
                             lower_own_priority=False)

    def status(self) -> dict[str, Any]:
        doc = read_archive_status(self.cfg.data_path / "archive_status.json")
        assert doc is not None
        return doc

    def drive_id(self, label: str) -> str:
        return self.ledger.query("SELECT drive_id FROM drives WHERE label=?", (label,))[0]["drive_id"]


@pytest.fixture()
def env(tmp_path: Path):  # noqa: ANN201
    e = Env(tmp_path)
    yield e
    e.ledger.close()


def _events(env: Env, code: str) -> list[Any]:
    return env.ledger.query("SELECT * FROM events WHERE code=?", (code,))


def test_full_drive_rolls_over_to_the_other_drive_then_swap_takes_out_the_full_one(env: Env) -> None:
    ids = add_synthetic_run(env.ledger, env.cfg, 4, SEG, sidecars=True)
    # the drive used first (lowest key) holds two recordings (with sidecars, index and README), not three
    a, b = sorted([make_drive(env.tmp, "A"), make_drive(env.tmp, "B")], key=lambda t: t.drive_key)
    io = CappedIO({Path(a.root): 2 * SEG + 60_000})
    bays = Bays(a, b)
    w = env.worker(bays, io)
    out = drain(w)
    assert out[-1] in ("full", "copied", "idle")
    for _ in range(5):                       # full -> next step moves on to B and copies the rest
        out = drain(w)
        if out[-1] == "idle":
            break
    assert out[-1] == "idle"
    la, lb = a.label, b.label
    da, db = env.drive_id(la), env.drive_id(lb)
    on_a = {r["segment_id"] for r in env.ledger.query(
        "SELECT segment_id FROM archive_copies WHERE drive_id=? AND kind='original'", (da,))}
    on_b = {r["segment_id"] for r in env.ledger.query(
        "SELECT segment_id FROM archive_copies WHERE drive_id=? AND kind='original'", (db,))}
    assert len(on_a) == 2 and on_a | on_b == set(ids) and not on_a & on_b      # every recording, once
    assert w.att is not None and w.att.drive_id == db
    # moving on is not a surprise removal (no A-003) and is recorded
    assert len(_events(env, "archive_drive_rollover")) == 1
    assert not _events(env, "archive_drive_removed")
    assert "A-003" not in {x["code"] for x in env.status()["alerts"]}
    check_drive(Path(a.root), env.ledger, da, complete=True)      # A's index was written when it filled up
    st = env.status()
    uses = {d["label"]: d["use"] for d in st["drives"]}
    assert uses == {la: "full", lb: "in_use"}
    assert f"{la} is full; copying continues on {lb}" in st["message"]

    # Swap: the FULL drive is prepared, copying stays on B
    w.request_swap()
    assert w.step() == "swapped"
    assert w.att is not None and w.att.drive_id == db
    add_synthetic_run(env.ledger, env.cfg, 1, SEG)
    assert drain(w)[-1] == "idle"
    assert len(claimed(env.ledger, db)) > 2 * len(on_b)          # the new recording went to B
    st = env.status()
    assert st["removal"]["label"] == la and st["state"] == "UP_TO_DATE"
    assert f"{la} is ready to be taken out" in st["message"] and "Safely Remove Hardware" in st["message"]
    assert {d["label"]: d["use"] for d in st["drives"]} == {lb: "in_use", la: "take_out"}
    # A taken out of its bay: forgotten after two scans, B never interrupted
    bays.targets = [b]
    w.step()
    w.step()
    st = env.status()
    assert st["removal"] is None and [d["label"] for d in st["drives"]] == [lb]
    assert not _events(env, "archive_drive_removed")


def test_a_full_drive_left_in_the_dock_stays_full_until_space_is_freed(env: Env) -> None:
    add_synthetic_run(env.ledger, env.cfg, 3, SEG)
    a, b = sorted([make_drive(env.tmp, "A"), make_drive(env.tmp, "B")], key=lambda t: t.drive_key)
    io = CappedIO({Path(a.root): SEG + 40_000})
    clock = [1000.0]
    w = ArchiveWorker(env.cfg, ledger=env.ledger, locator=Bays(a, b), io=io, allow_same_volume=True,
                      lower_own_priority=False, mono=lambda: clock[0])
    for _ in range(4):
        drain(w)
    assert w.att is not None and w.att.target.label == b.label
    clock[0] += 3600                                   # long after the recheck interval
    add_synthetic_run(env.ledger, env.cfg, 1, SEG)
    drain(w)
    assert {d["label"]: d["use"] for d in env.status()["drives"]} == {a.label: "full", b.label: "in_use"}
    w.request_swap()
    assert w.step() == "swapped" and env.status()["removal"]["label"] == a.label   # the full one, not B
    assert w.att is not None and w.att.target.label == b.label


def test_every_drive_full_stays_full_with_the_alert(env: Env) -> None:
    add_synthetic_run(env.ledger, env.cfg, 3, SEG)
    a, b = make_drive(env.tmp, "A"), make_drive(env.tmp, "B")
    io = CappedIO({Path(a.root): SEG + 40_000, Path(b.root): SEG + 40_000})   # one recording each
    w = env.worker(Bays(a, b), io)
    for _ in range(6):
        out = drain(w)
    assert out[-1] == "full"
    st = env.status()
    assert st["state"] == "FULL" and "A-002" in {x["code"] for x in st["alerts"]}
    assert len(_events(env, "archive_drive_rollover")) == 1      # A -> B once, never back and forth


def test_sidecars_go_to_fingerprints_and_day_folders_hold_only_video(env: Env) -> None:
    add_synthetic_run(env.ledger, env.cfg, 3, SEG, sidecars=True)
    a = make_drive(env.tmp, "A")
    w = env.worker(Bays(a), CappedIO({}))
    drain(w)
    rows = env.ledger.query("SELECT kind, path FROM archive_copies")
    cam = f"Sentinel/{env.cfg.camera.id}"
    for r in rows:
        if r["kind"] == "framehash":
            assert r["path"].startswith(f"{cam}/Fingerprints/") and r["path"].endswith(".framehash.txt")
        else:
            assert not r["path"].startswith(f"{cam}/Fingerprints/") and r["path"].endswith(".ts")
    media = {Path(r["path"]).name[: -len(".ts")] for r in rows if r["kind"] == "original"}
    sidecars = {Path(r["path"]).name[: -len(".framehash.txt")] for r in rows if r["kind"] == "framehash"}
    assert media == sidecars                                    # same name, so they are easy to pair
    for d in (Path(a.root) / cam).iterdir():
        if d.name[:2] == "20":                                  # a day folder
            assert {p.suffix for p in d.iterdir()} == {".ts"}


def test_quarantine_sidecars_stay_under_quarantine() -> None:
    seg = {"camera_id": "CAM01", "segment_id": "abcdef12-0000", "state": "CORRUPT", "run_id": "r", "seq": 1,
           "start_utc": "2026-11-11T14:00:00.000000Z", "end_utc": "2026-11-11T15:00:00.000000Z", "rel_path": "x.ts"}
    assert layout.media_rel_path(seg) == "Sentinel/CAM01/Quarantine/2026-11-11/140000Z_150000Z_abcdef12.ts"
    assert layout.framehash_rel_path(seg) == ("Sentinel/CAM01/Quarantine/Fingerprints/2026-11-11/"
                                              "140000Z_150000Z_abcdef12.framehash.txt")


def _entry(path: str, start: str, end: str, kind: str = "original") -> dict[str, Any]:
    return {"path": path, "kind": kind, "start_utc": start, "end_utc": end, "duration_s": 3600.0}


def test_playlists_follow_the_local_day_in_time_order() -> None:
    tz = ZoneInfo("America/New_York")
    c = "Sentinel/CAM01"
    entries = [
        _entry(f"{c}/2026-11-12/010000Z_020000Z_bbbbbbbb.ts", "2026-11-12T01:00:00.000000Z", "2026-11-12T02:00:00.000000Z"),
        _entry(f"{c}/2026-11-11/230000Z_000000Z_aaaaaaaa.ts", "2026-11-11T23:00:00.000000Z", "2026-11-12T00:00:00.000000Z"),
        _entry(f"{c}/2026-11-12/060000Z_070000Z_cccccccc.ts", "2026-11-12T06:00:00.000000Z", "2026-11-12T07:00:00.000000Z"),
        _entry(f"{c}/Fingerprints/2026-11-12/x.framehash.txt", "2026-11-12T06:00:00.000000Z", "", "framehash"),
        _entry(f"{c}/Quarantine/2026-11-12/y.ts", "2026-11-12T03:00:00.000000Z", "", "quarantine"),
    ]
    files = idx.build_playlists("CAM01", entries, tz)
    # 23:00Z and 01:00Z are the evening of 11 November in New York; 06:00Z is 01:00 on the 12th
    assert sorted(files) == [f"{c}/Playlists/2026-11-11.m3u", f"{c}/Playlists/2026-11-12.m3u"]
    text = files[f"{c}/Playlists/2026-11-11.m3u"].decode("ascii")
    lines = text.split("\r\n")
    assert lines[0] == "#EXTM3U" and text.endswith("\r\n")
    paths = [ln for ln in lines if ln and not ln.startswith("#")]
    assert paths == ["../2026-11-11/230000Z_000000Z_aaaaaaaa.ts", "../2026-11-12/010000Z_020000Z_bbbbbbbb.ts"]
    assert "#EXTINF:3600,CAM01 2026-11-11 18:00:00-19:00:00 EST" in lines
    other = files[f"{c}/Playlists/2026-11-12.m3u"].decode("ascii")
    assert "060000Z_070000Z_cccccccc.ts" in other and "framehash" not in other and "Quarantine" not in other


def test_worker_writes_playlists_that_point_at_the_verified_copies(env: Env) -> None:
    add_synthetic_run(env.ledger, env.cfg, 3, SEG, sidecars=True)
    a = make_drive(env.tmp, "A")
    w = env.worker(Bays(a), CappedIO({}))
    drain(w)
    w.request_swap()
    assert w.step() == "swapped"
    pdir = Path(a.root) / layout.playlist_dir(env.cfg.camera.id)
    lists = sorted(pdir.glob("*.m3u"))
    assert lists
    listed = []
    for pl in lists:
        for ln in pl.read_text("ascii").splitlines():
            if ln and not ln.startswith("#"):
                target = (pl.parent / ln).resolve()
                assert target.is_file()
                listed.append(target)
    assert len(listed) == 3
    # not evidence: the checksums list does not name them
    sums = (Path(a.root) / layout.index_files(env.cfg.camera.id)["sums"]).read_text("utf-8")
    assert ".m3u" not in sums
    before = {p: p.stat().st_mtime_ns for p in lists}
    w2 = env.worker(Bays(a), CappedIO({}))                      # attach again: unchanged lists are not rewritten
    w2.resume()
    drain(w2)
    assert {p: p.stat().st_mtime_ns for p in lists} == before


@pytest.mark.skipif(os.name != "nt", reason="drive text formatting only")
def test_window_lists_both_drives() -> None:
    from sentinel.gui.app.drive_pages import archive_drives_text

    arc = {"drive_label": "ARCHIVE_B", "drives": [
        {"label": "ARCHIVE_A", "use": "full", "free_bytes": 0},
        {"label": "ARCHIVE_B", "use": "in_use", "free_bytes": 1_200_000_000_000}]}
    text = archive_drives_text(arc)
    assert text.startswith("Full (swap it): ARCHIVE_A.") and "In use: ARCHIVE_B (" in text
    assert archive_drives_text({"drive_label": None}) == "Drive: none inserted."
