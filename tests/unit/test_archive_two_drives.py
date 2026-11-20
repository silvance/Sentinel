"""Archive drive, owner request 2026-11-11: two drives in a two-bay dock (copying moves on to the other
drive when one is full; Swap then takes out the full one), sidecars in Fingerprints/, and one playlist
per local day."""

from __future__ import annotations

import os
import stat
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
    # 2026-11-17: the Drives page removes exactly that drive; Windows' own icon is not named (two-bay docks)
    assert f"{la} is ready to be taken out" in st["message"] and "Safely Remove Hardware" not in st["message"]
    assert "Press Safely remove on Sentinel's Drives page" in st["message"]
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
    # 23:00Z and 01:00Z are the evening of 11 November in New York; 06:00Z is 01:00 on the 12th.
    # 0.3.22: in the folder for people at the top of the drive, named by date and weekday.
    wed, thu = ("1 - Watch the recordings/2026-11-11 Wednesday - CAM01.m3u",
                "1 - Watch the recordings/2026-11-12 Thursday - CAM01.m3u")
    assert sorted(files) == [wed, thu]
    text = files[wed].decode("ascii")
    lines = text.split("\r\n")
    assert lines[0] == "#EXTM3U" and text.endswith("\r\n")
    paths = [ln for ln in lines if ln and not ln.startswith("#")]
    assert paths == ["../Sentinel/CAM01/2026-11-11/230000Z_000000Z_aaaaaaaa.ts",
                     "../Sentinel/CAM01/2026-11-12/010000Z_020000Z_bbbbbbbb.ts"]
    assert "#EXTINF:3600,CAM01 2026-11-11 18:00:00-19:00:00 EST" in lines
    other = files[thu].decode("ascii")
    assert "060000Z_070000Z_cccccccc.ts" in other and "framehash" not in other and "Quarantine" not in other


def test_the_names_for_people_do_not_look_like_a_review_drive() -> None:
    """A review drive is recognised by README_FIRST.txt, Recordings, Manifests, Originals at its top (and an
    archive drive with those is refused as one). The way in for people must not use those names."""
    from sentinel.archive.worker import REVIEW_LAYOUT
    from sentinel.drives.marker import REVIEW_DIRS

    top = {layout.WATCH_DIR.lower(), layout.START_HERE.lower()}
    assert not top & {n.lower() for n in REVIEW_LAYOUT} and not top & set(REVIEW_DIRS)
    assert layout.WATCH_DIR < layout.ROOT                        # sorts before "Sentinel" in the folder list
    assert layout.playlist_rel_path("CAM 1", "2026-11-18") == "1 - Watch the recordings/2026-11-18 Wednesday - CAM_1.m3u"
    assert layout.playlist_rel_path("CAM01", "undated") == "1 - Watch the recordings/undated - CAM01.m3u"
    notes = idx.people_files()
    assert set(notes) == {"START HERE.txt", "1 - Watch the recordings/How to watch.txt"}
    for data in notes.values():
        text = data.decode("ascii")                              # plain ASCII, Windows line ends
        assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert b'Open the folder "1 - Watch the recordings"' in notes["START HERE.txt"]
    assert b'Leave the folder "Sentinel" alone' in notes["START HERE.txt"]


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
    # 0.3.22: they are the first thing on the drive, with the two notes for people; the day folders are not
    assert pdir == Path(a.root) / "1 - Watch the recordings" and (pdir / "How to watch.txt").is_file()
    assert (Path(a.root) / "START HERE.txt").read_bytes() == idx.people_files()["START HERE.txt"]
    assert all(p.name.endswith(f" - {env.cfg.camera.id}.m3u") and p.name[11:].split(" ")[0] in layout.WEEKDAYS
               for p in lists)
    assert not (Path(a.root) / layout.old_playlist_dir(env.cfg.camera.id)).exists()
    assert ".txt" not in sums.replace(".framehash.txt", "")     # the notes are not evidence either
    before = {p: p.stat().st_mtime_ns for p in [*lists, Path(a.root) / "START HERE.txt", pdir / "How to watch.txt"]}
    w2 = env.worker(Bays(a), CappedIO({}))                      # attach again: unchanged lists are not rewritten
    w2.resume()
    drain(w2)
    assert {p: p.stat().st_mtime_ns for p in before} == before


def test_playlists_left_by_an_earlier_version_are_replaced(env: Env) -> None:
    """A drive written by 0.3.21 or earlier has Sentinel/<camera>/Playlists/<date>.m3u beside the day
    folders. The next index write puts the playlists at the top of the drive and removes the old set, and
    nothing else: the recordings, the index and other files in that folder stay."""
    add_synthetic_run(env.ledger, env.cfg, 3, SEG, sidecars=True)
    a = make_drive(env.tmp, "A")
    old = Path(a.root) / layout.old_playlist_dir(env.cfg.camera.id)
    old.mkdir(parents=True)
    (old / "2026-11-10.m3u").write_text("#EXTM3U\r\n../2026-11-10/x.ts\r\n", "ascii")
    (old / "2026-11-11.m3u").write_text("#EXTM3U\r\n", "ascii")
    os.chmod(old / "2026-11-11.m3u", stat.S_IREAD)              # read-only, as files on the drive often are
    (old / "notes of an examiner.txt").write_text("keep me", "ascii")
    w = env.worker(Bays(a), CappedIO({}))
    drain(w)
    w.request_swap()
    assert w.step() == "swapped"
    assert sorted(p.name for p in old.iterdir()) == ["notes of an examiner.txt"]     # only the old playlists went
    new = sorted((Path(a.root) / layout.WATCH_DIR).glob("*.m3u"))
    assert new and all((p.parent / ln).resolve().is_file() for p in new
                       for ln in p.read_text("ascii").splitlines() if ln and not ln.startswith("#"))
    originals = [p for p in (Path(a.root) / layout.ROOT).rglob("*.ts")]
    assert len(originals) == 3


@pytest.mark.skipif(os.name != "nt", reason="drive text formatting only")
def test_window_lists_both_drives() -> None:
    from sentinel.gui.app.drive_pages import archive_drive_lines

    arc = {"drive_label": "ARCHIVE_B", "drives": [
        {"label": "ARCHIVE_A", "use": "full", "free_bytes": 0},
        {"label": "ARCHIVE_B", "use": "in_use", "free_bytes": 1_200_000_000_000}]}
    # one line per drive (0.3.18; it was one sentence: "Full (swap it): ARCHIVE_A. In use: ARCHIVE_B (…)")
    assert archive_drive_lines(arc) == [("full", "ARCHIVE_A", "Full: press Swap and safely remove"),
                                        ("in_use", "ARCHIVE_B", "In use · 1.2 TB free")]
    assert archive_drive_lines({"drive_label": None}) == []


@pytest.mark.skipif(os.name != "nt", reason="drive text formatting only")
def test_window_says_what_a_second_drive_does() -> None:
    """End-user request 2026-11-17: nothing on the screen said that copying moves on to a second drive by itself."""
    from sentinel.gui.app.drive_pages import archive_drive_lines, second_drive_hint

    a = {"label": "SSD_1", "use": "in_use", "full": False, "free_bytes": 10**12}
    b = {"label": "SSD_2", "use": "standby", "full": False, "free_bytes": 10**12}
    one = second_drive_hint({"running": True, "drives": [a]})
    assert "second archive drive can stay plugged in" in one and "when SSD_1 is full" in one
    two = second_drive_hint({"running": True, "drives": [a, b]})
    assert two.startswith("When SSD_1 is full, copying moves to SSD_2 by itself.")
    assert archive_drive_lines({"drives": [a, b]})[1] == ("standby", "SSD_2", "Standing by · 1.0 TB free")
    full = second_drive_hint({"running": True, "drives": [dict(a, full=True)]})
    assert "plug in a second archive drive that has room" in full
    # nothing to add: not running, no drive, already moved on, a swap under way, a drive back from the office
    moved = [dict(a, use="full", full=True), dict(b, use="in_use")]
    for arc in ({"running": False, "drives": [a]}, {"running": True, "drives": []},
                {"running": True, "drives": moved},
                {"running": True, "drives": [a], "removal": {"label": "SSD_2"}},
                {"running": True, "drives": [a], "awaiting_return": [{"label": "SSD_2"}]}):
        assert second_drive_hint(arc) == ""


def test_status_heartbeat_continues_while_one_long_file_is_copied_and_read_back(env: Env, tmp_path: Path) -> None:
    """Field report 2026-11-11: a one-hour file takes minutes to copy and read back; the status file must
    stay fresh meanwhile, or the window says "Archive copying is not running" (A-007)."""
    clock = [1000.0]

    def mono() -> float:
        clock[0] += 4.0          # every look at the clock is 4 s later: a slow copy
        return clock[0]

    w = ArchiveWorker(env.cfg, ledger=env.ledger, locator=Bays(), io=ArchiveIO(read_chunk=1024 * 1024),
                      allow_same_volume=True, lower_own_priority=False, mono=mono)
    beats: list[float] = []
    real = w.status.write
    w.status.write = lambda **f: beats.append(clock[0]) or real(**f)  # type: ignore[method-assign]
    big = tmp_path / "hour.ts"
    big.write_bytes(os.urandom(8 * 1024 * 1024))        # 8 chunks of 1 MiB
    assert sum(len(b) for b in w._source_chunks(big)) == big.stat().st_size
    copied = len(beats)
    assert copied >= 2
    w._read_hash(big)
    assert len(beats) - copied >= 2
    assert env.status()["heartbeat_epoch"] > 0


def test_a_second_swap_press_never_prepares_the_drive_in_use_or_reuses_the_waiting_one(env: Env) -> None:
    """Review 2026-11-12: with the full drive waiting to be taken out, another Swap press was armed and then
    prepared the drive in use, while the full drive was attached and written again."""
    add_synthetic_run(env.ledger, env.cfg, 4, SEG)
    a, b = sorted([make_drive(env.tmp, "A"), make_drive(env.tmp, "B")], key=lambda t: t.drive_key)
    io = CappedIO({Path(a.root): 2 * SEG + 60_000})
    bays = Bays(a, b)
    w = env.worker(bays, io)
    for _ in range(6):
        if drain(w)[-1] == "idle":
            break
    db = env.drive_id(b.label)
    w.request_swap()
    assert w.step() == "swapped" and w.att is not None and w.att.drive_id == db
    w.request_swap()                                  # pressed again: ignored while A waits to be taken out
    add_synthetic_run(env.ledger, env.cfg, 1, SEG)
    assert drain(w)[-1] == "idle"
    assert w.att is not None and w.att.drive_id == db
    st = env.status()
    assert st["removal"]["label"] == a.label
    assert {d["label"]: d["use"] for d in st["drives"]} == {b.label: "in_use", a.label: "take_out"}


def test_one_drive_a_second_press_does_not_prepare_the_next_drive_inserted(env: Env) -> None:
    add_synthetic_run(env.ledger, env.cfg, 1, SEG)
    a, b = make_drive(env.tmp, "A"), make_drive(env.tmp, "B")
    bays = Bays(a)
    w = env.worker(bays, CappedIO({}))
    drain(w)
    w.request_swap()
    for _ in range(10):
        if w.step() == "swapped":
            break
    w.request_swap()                                  # pressed again while A is still in its bay
    assert not w._swap.is_set()
    bays.targets = [a, b]                             # the next drive goes into the other bay first
    add_synthetic_run(env.ledger, env.cfg, 1, SEG)
    drain(w)
    assert w.att is not None and w.att.drive_id == env.drive_id(b.label)   # B in use, A not written again
    assert env.status()["removal"]["label"] == a.label


def test_a_refused_drive_does_not_block_the_good_drive_next_to_it(env: Env) -> None:
    """First review run, 2026-11-12: the lowest drive was chosen at every scan, refused, and the other one never
    tried."""
    from sentinel.archive.worker import REVIEW_LAYOUT

    add_synthetic_run(env.ledger, env.cfg, 2, SEG)
    a, b = sorted([make_drive(env.tmp, "A"), make_drive(env.tmp, "B")], key=lambda t: t.drive_key)
    (Path(a.root) / REVIEW_LAYOUT[0]).mkdir()          # the first drive holds a review-drive layout
    w = env.worker(Bays(a, b), CappedIO({}))
    out = [w.step() for _ in range(4)]
    assert out[0] == "unusable" and "copied" in out
    assert w.att is not None and w.att.drive_id == b.drive_key


def test_one_file_that_cannot_be_read_back_does_not_stop_all_copying(env: Env) -> None:
    """First review run, 2026-11-12: an I/O error reading one copy back was retried for ever and nothing else
    was copied; now it is reported (A-006) and skipped after the usual attempts."""
    import errno

    class BadSector(CappedIO):
        def __init__(self) -> None:
            super().__init__({})
            self.bad: str | None = None

        def read_back(self, path: Path):  # noqa: ANN201
            if path.suffix == ".ts" and (self.bad is None or self.bad == path.name):
                self.bad = path.name
                raise OSError(errno.EIO, "I/O error")
            return super().read_back(path)

    add_synthetic_run(env.ledger, env.cfg, 3, SEG)
    a = make_drive(env.tmp, "A")
    io = BadSector()
    w = env.worker(Bays(a), io)
    out = []
    for _ in range(12):
        try:
            out.append(w.step())
        except OSError:
            out.append("error")
    assert out.count("error") == env.cfg.archive.verify_attempts - 1
    copied = env.ledger.query("SELECT COUNT(*) FROM archive_copies WHERE kind='original' AND state='VERIFIED'")
    assert copied[0][0] == 2                                      # the other two recordings were copied
    probs = env.ledger.query("SELECT kind FROM archive_problems")
    assert [r[0] for r in probs] == ["verify_failed"]             # A-006 names the file


def test_the_wrong_drive_pulled_after_a_swap_is_said_on_the_screen(env: Env) -> None:
    """Owner's question 2026-11-20: an agent presses Swap and safely remove for drive 1, then pulls drive 2 by
    mistake. Until 0.3.28 the page showed no alert and went on asking for drive 1 to be taken out; only the log
    had "removed without preparing it". Nothing is lost either way: recordings wait on the computer."""
    add_synthetic_run(env.ledger, env.cfg, 3, SEG, sidecars=True)
    a, b = sorted([make_drive(env.tmp, "A"), make_drive(env.tmp, "B")], key=lambda t: t.drive_key)
    bays = Bays(a, b)
    w = env.worker(bays, ArchiveIO(read_chunk=1024 * 1024))
    assert drain(w)[-1] == "idle"
    one = a if env.status()["drive_label"] == a.label else b        # drive 1: in use, the one to be swapped
    two = b if one is a else a

    def look() -> dict[str, Any]:
        w._last_publish = float("-inf")         # the copier writes its status once a second; the test is faster
        w.step()
        return env.status()

    w.request_swap()
    assert w.step() == "swapped"
    st = look()
    assert st["alerts"] == [] and f"{one.label} is ready to be taken out" in st["message"]
    assert [(d["label"], d["use"]) for d in st["drives"]] == [(two.label, "in_use"), (one.label, "take_out")]

    bays.targets.remove(two)                    # THE MISTAKE
    w.step()
    add_synthetic_run(env.ledger, env.cfg, 2, SEG, sidecars=True)   # recording goes on meanwhile
    st = look()
    assert st["state"] == "REMOVE_MANUALLY"
    assert st["message"].startswith(f"{two.label} was unplugged, but the drive to take out is {one.label}. "
                                    f"Plug {two.label} back in")
    assert "Nothing is lost" in st["message"]
    assert [(x["code"], x["severity"]) for x in st["alerts"]] == [("A-003", 2)]
    assert st["backlog"]["segments"] == 2       # and what waits to be copied is counted
    assert len(_events(env, "archive_drive_removed")) == 1

    bays.targets.append(two)                    # plugged back in: copying carries on by itself
    assert drain(w)[-1] == "idle"
    st = look()
    assert st["alerts"] == [] and st["backlog"]["segments"] == 0
    assert f"{one.label} is ready to be taken out" in st["message"] and "was unplugged" not in st["message"]
    assert [(d["label"], d["use"]) for d in st["drives"]] == [(two.label, "in_use"), (one.label, "take_out")]

    # Pulled again, and this time the prepared drive is taken out as well: no drive at all, as before 0.3.28.
    bays.targets.remove(two)
    look()
    bays.targets.remove(one)
    look()
    st = look()
    assert st["state"] == "ABSENT" and [x["code"] for x in st["alerts"]] == ["A-003"]
    assert "was unplugged, but" not in st["message"]
