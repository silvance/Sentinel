"""Medium findings of the 2026-11-12 overnight review, archive copier: docs/10 step 1 with two SSDs, swap texts
that promised SAFE TO REMOVE, Cancel swap releasing drives at the office, the one-scan debounce with a second
drive inserted, a swap pressed while the drive is refused, a reconcile error on attach, and a Return to service
that could not finish (files it could not delete, or an interruption after the transfer was recorded)."""

from __future__ import annotations

import errno
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from archive_helpers import check_drive, drain, make_drive
from conftest import make_config
from sentinel.alerts import CATALOG
from sentinel.archive import __main__ as arc_cli
from sentinel.archive import layout
from sentinel.archive.io import ArchiveIO
from sentinel.archive.locator import DriveScan
from sentinel.archive.worker import CONTROL_DIR, MSG_FULL, SWAP_REQUEST, ArchiveWorker
from sentinel.archive.status import STATUS_FILE
from sentinel.db import Ledger
from sentinel.export.fileio import PARTIAL
from test_archive_return import OneBay, _request, _result, _states, _to_office_and_back, _videos
from test_archive_two_drives import SEG, Bays, CappedIO, Env
from tools.archive_bench import add_synthetic_run


@pytest.fixture()
def env(tmp_path: Path):  # noqa: ANN201
    e = Env(tmp_path)
    yield e
    e.ledger.close()


@pytest.fixture()
def one(tmp_path: Path):  # noqa: ANN201
    """One-bay dock, this_drive policy (as tests/unit/test_archive_return.py)."""
    cfg = make_config(tmp_path, archive={
        "enabled": True, "drive_path": str(tmp_path / "unused"), "require_drive_marker": False,
        "reserve_free_mb": 0.0, "poll_interval_s": 0.05, "chunk_mib": 1, "fsync_every_mib": 2,
        "max_rate_mb_s": 1000.0, "resume_policy": "this_drive"})
    led = Ledger(cfg.data_path / "sentinel.db")
    drive = make_drive(tmp_path, "A", marker=True)
    w = _worker(cfg, led, OneBay(drive))
    yield cfg, led, drive, w
    led.close()


def _worker(cfg: Any, led: Ledger, locator: Any, io: ArchiveIO | None = None) -> ArchiveWorker:
    return ArchiveWorker(cfg, ledger=led, locator=locator, io=io or ArchiveIO(read_chunk=1024 * 1024),
                         allow_same_volume=True, lower_own_priority=False)


def _events(led: Ledger, code: str) -> list[Any]:
    return led.query("SELECT * FROM events WHERE code=?", (code,))


def _swap(w: ArchiveWorker) -> None:
    w.request_swap()
    for _ in range(20):
        if w.step() == "swapped":
            return
    raise AssertionError("the swap did not finish")


def _office_check(root: Path) -> list[str]:
    """What the docs/10 step 5 PowerShell check reports: PROBLEM and NOT LISTED lines."""
    listed: dict[str, str] = {}
    for sums in root.rglob("SHA256SUMS.txt"):
        for ln in sums.read_text("utf-8").splitlines():
            if ln:
                h, rel = ln.split("  ", 1)
                listed[rel] = h
    out = []
    for rel in listed:
        if not (root / rel).is_file():
            out.append(f"PROBLEM: {rel}")
    for p in root.rglob("*"):
        if p.is_file() and (p.suffix == ".ts" or p.name.endswith(layout.FRAMEHASH_SUFFIX)):
            if p.relative_to(root).as_posix() not in listed:
                out.append(f"NOT LISTED: {p.relative_to(root).as_posix()}")
    return out


# ---------------------------------------------------------------------- 1. docs/10 step 1, two SSDs
def test_docs10_step1_with_two_ssds_takes_out_the_ssd_that_was_unplugged(env: Env) -> None:
    """Following docs/10 step 1 for two SSDs connected (Swap, take nothing out, wait until the re-plugged SSD is
    in use, Cancel swap, Swap) prepares the SSD that was unplugged, with a complete index; the other SSD keeps
    being used and is not left waiting for a return."""
    add_synthetic_run(env.ledger, env.cfg, 2, SEG)
    x, y = sorted([make_drive(env.tmp, "A"), make_drive(env.tmp, "B")], key=lambda t: t.drive_key)
    bays = Bays(x, y)
    w = env.worker(bays, CappedIO({}))
    drain(w)
    assert w.att is not None and w.att.drive_id == x.drive_key
    xr = Path(x.root)
    yanked = xr.with_name(xr.name + "_yanked")
    xr.rename(yanked)                                  # unplugged without Swap
    add_synthetic_run(env.ledger, env.cfg, 1, SEG)
    for _ in range(4):
        w.step()
    drain(w)
    assert w.att is not None and w.att.drive_id == y.drive_key
    yanked.rename(xr)                                  # plugged back in, next to the other SSD
    add_synthetic_run(env.ledger, env.cfg, 1, SEG)
    drain(w)
    assert w.att.drive_id == y.drive_key               # sticky: the re-plugged SSD only stands by

    w.request_swap()                                   # Swap once: it prepares the SSD in use (take nothing out)
    assert w.step() == "swapped"
    assert env.status()["removal"]["label"] == y.label
    drain(w)
    uses = {d["label"]: d["use"] for d in env.status()["drives"]}
    assert uses[x.label] == "in_use"                   # the re-plugged SSD is in use now
    w.resume()                                         # Cancel swap
    drain(w)
    assert w.att is not None and w.att.drive_id == x.drive_key
    _swap(w)                                           # Swap again: now the screen names the re-plugged SSD
    st = env.status()
    assert st["removal"]["label"] == x.label and f"{x.label} is ready to be taken out" in st["message"]
    check_drive(xr, env.ledger, x.drive_key, complete=True)
    assert _office_check(xr) == []
    bays.targets = [y]                                 # the re-plugged SSD goes to the office
    add_synthetic_run(env.ledger, env.cfg, 1, SEG)
    w.step()
    w.step()
    drain(w)
    assert w.att is not None and w.att.drive_id == y.drive_key
    assert y.drive_key not in w._awaiting and x.drive_key in w._awaiting


# ---------------------------------------------------------------------- 2. no SAFE TO REMOVE promised
def test_swap_instructions_do_not_promise_safe_to_remove(env: Env) -> None:
    """The deployed copier has no software eject: the screen ends in REMOVE_MANUALLY, never SAFE TO REMOVE, so
    the A-002 action and the 'full' message must not tell the agent to wait for those words."""
    assert "SAFE TO REMOVE" not in CATALOG["A-002"].action.upper()
    assert "SAFE TO REMOVE" not in MSG_FULL.upper()
    assert "which drive to take out" in CATALOG["A-002"].action and "which drive to take out" in MSG_FULL
    add_synthetic_run(env.ledger, env.cfg, 1, SEG)
    a = make_drive(env.tmp, "A")
    w = env.worker(Bays(a), CappedIO({}))             # no remover: ManualRemovalFallback, as in production
    drain(w)
    _swap(w)
    st = env.status()
    assert st["state"] == "REMOVE_MANUALLY" and "SAFE TO REMOVE" not in st["message"]


# ---------------------------------------------------------------------- 3. Cancel swap
def test_cancel_swap_keeps_a_drive_at_the_office_waiting_for_its_return(one, tmp_path: Path) -> None:  # noqa: ANN001
    cfg, led, ssd1, w = one
    ssd2 = make_drive(tmp_path, "B", marker=True)
    add_synthetic_run(led, cfg, 1, SEG)
    drain(w)
    _swap(w)                                           # SSD_1 swapped and taken to the office
    r1 = Path(ssd1.root)
    office = r1.with_name(r1.name + "_at_office")
    r1.rename(office)
    w.locator = OneBay(ssd2)
    add_synthetic_run(led, cfg, 1, SEG)
    w.step()
    w.step()
    drain(w)
    assert w.att is not None and w.att.drive_id == ssd2.drive_key
    _swap(w)                                           # Swap pressed by mistake ...
    w.resume()                                         # ... and Cancel swap
    add_synthetic_run(led, cfg, 1, SEG)
    drain(w)
    assert w.att is not None and w.att.drive_id == ssd2.drive_key          # SSD_2 in use again
    assert ssd1.drive_key in w._awaiting and ssd2.drive_key not in w._awaiting
    saved = json.loads((cfg.data_path / CONTROL_DIR / "awaiting_return.json").read_text("utf-8"))
    assert list(saved["drives"]) == [ssd1.drive_key]

    office.rename(r1)                                  # SSD_1 back from the office (one bay: SSD_2 out)
    w.locator = OneBay(ssd1)
    before = {p.name for p in _videos(r1)}
    add_synthetic_run(led, cfg, 1, SEG)
    assert w.step() == "rescan"
    assert w.step() == "absent" and w.att is None      # not copied to before its return
    assert {p.name for p in _videos(r1)} == before
    st = json.loads((cfg.data_path / STATUS_FILE).read_text("utf-8"))
    assert [d["label"] for d in st["awaiting_return"]] == [ssd1.label]
    _request(cfg, ssd1)
    w.step()
    assert _result(cfg)["ok"], _result(cfg)


def test_cancel_swap_releases_an_awaiting_drive_inserted_at_that_moment(one) -> None:  # noqa: ANN001
    """The release that Cancel swap is for (keep using the drive in hand) still works, whether the request is read
    before or after the scan that sees the drive."""
    cfg, led, drive, w = one
    add_synthetic_run(led, cfg, 1, SEG)
    drain(w)
    _to_office_and_back(w, drive)
    assert w.step() == "absent" and drive.drive_key in w._awaiting_present
    w.resume()
    assert drive.drive_key not in w._awaiting
    add_synthetic_run(led, cfg, 1, SEG)
    assert drain(w)[-1] == "idle" and w.att is not None


# ---------------------------------------------------------------------- 4. one missed scan, second drive inserted
class Flaky:
    """Two bays; ``hide`` drops drives from the next ``times`` scans."""

    def __init__(self, bays: Bays) -> None:
        self.bays = bays
        self.hide: set[str] = set()
        self.times = 0

    def scan(self) -> DriveScan:
        s = self.bays.scan()
        if self.times > 0:
            self.times -= 1
            return DriveScan([t for t in s.candidates if t.drive_key not in self.hide])
        return s


def test_one_missed_scan_with_a_second_drive_inserted_is_not_a_removal(env: Env) -> None:
    add_synthetic_run(env.ledger, env.cfg, 2, SEG)
    a, b = sorted([make_drive(env.tmp, "A"), make_drive(env.tmp, "B")], key=lambda t: t.drive_key)
    loc = Flaky(Bays(a, b))
    w = env.worker(loc, CappedIO({}))
    drain(w)
    assert w.att is not None and w.att.drive_id == a.drive_key
    add_synthetic_run(env.ledger, env.cfg, 1, SEG)
    loc.hide, loc.times = {a.drive_key}, 1
    assert w.step() == "rescan"
    assert w.att is not None and w.att.drive_id == a.drive_key
    assert not _events(env.ledger, "archive_drive_removed")
    sessions = env.ledger.query("SELECT gone_utc FROM archive_drive_sessions WHERE drive_id=?", (a.drive_key,))
    assert [r["gone_utc"] for r in sessions] == [None]                  # A's presence session stays open
    assert drain(w)[-1] == "idle" and w.att.drive_id == a.drive_key     # carries on with A
    assert not _events(env.ledger, "archive_drive_removed")
    # gone for two scans: removed (A-003 event), copying moves on to B
    add_synthetic_run(env.ledger, env.cfg, 1, SEG)
    loc.times = 2
    assert w.step() == "rescan"
    w.step()
    assert w.att is not None and w.att.drive_id == b.drive_key
    assert len(_events(env.ledger, "archive_drive_removed")) == 1


def test_a_pending_swap_is_dropped_when_its_drive_is_removed_without_preparing(env: Env) -> None:
    add_synthetic_run(env.ledger, env.cfg, 1, SEG)
    a, b = sorted([make_drive(env.tmp, "A"), make_drive(env.tmp, "B")], key=lambda t: t.drive_key)
    bays = Bays(a, b)
    w = env.worker(bays, CappedIO({}))
    drain(w)
    bays.targets = [b]                                 # A pulled out ...
    w.request_swap()                                   # ... as Swap is pressed for it
    assert w.step() == "rescan"
    w.step()
    assert w.att is not None and w.att.drive_id == b.drive_key       # B is used, not prepared for removal
    assert not w._swap.is_set() and w._swapped_drive is None and not w._awaiting


# ---------------------------------------------------------------------- 5. swap while the drive is refused
def _refused_drive(env: Env, name: str) -> Any:
    from sentinel.archive.worker import REVIEW_LAYOUT

    d = make_drive(env.tmp, name)
    (Path(d.root) / REVIEW_LAYOUT[0]).mkdir()
    return d


def test_swap_pressed_while_the_drive_is_refused_never_prepares_the_good_drive(env: Env) -> None:
    add_synthetic_run(env.ledger, env.cfg, 2, SEG)
    a, b = sorted([make_drive(env.tmp, "A"), make_drive(env.tmp, "B")], key=lambda t: t.drive_key)
    (Path(a.root) / "Recordings").mkdir()              # the first drive holds a review-drive layout: refused
    bays = Bays(a, b)
    w = env.worker(bays, CappedIO({}))
    assert w.step() == "unusable"
    w.request_swap()                                   # the agent presses Swap to get rid of the refused drive
    assert not w._swap.is_set()
    out = [w.step() for _ in range(4)]
    assert "swapped" not in out and "copied" in out
    assert w.att is not None and w.att.drive_id == b.drive_key
    assert w._swapped_drive is None and not w._awaiting
    assert _events(env.ledger, "archive_swap_dropped")


def test_one_bay_swap_pressed_then_refused_drive_replaced_within_one_poll(env: Env) -> None:
    add_synthetic_run(env.ledger, env.cfg, 1, SEG)
    bad, good = _refused_drive(env, "A"), make_drive(env.tmp, "B")
    bays = Bays()
    w = env.worker(bays, CappedIO({}))
    assert w.step() == "absent"
    w.request_swap()                                   # pressed with no drive attached ...
    bays.targets = [bad]                               # ... and a drive that is refused goes in
    assert w.step() == "unusable" and not w._swap.is_set()
    bays.targets = [good]                              # replaced by a good drive within one poll
    out = drain(w)
    assert "swapped" not in out and w.att is not None and w.att.drive_id == good.drive_key
    assert w._swapped_drive is None and not w._awaiting


def test_swap_command_answers_at_once_when_the_inserted_drive_is_refused(tmp_path: Path, monkeypatch,
                                                                       capsys) -> None:  # noqa: ANN001
    from types import SimpleNamespace

    c = SimpleNamespace(data_path=tmp_path / "data")
    c.data_path.mkdir()
    monkeypatch.setattr(arc_cli, "_load", lambda _a: c)
    (c.data_path / STATUS_FILE).write_text(json.dumps({
        "state": "UNUSABLE", "heartbeat_epoch": time.time(),
        "message": "The inserted drive cannot be used as an archive drive."}), "utf-8")
    t0 = time.monotonic()
    assert arc_cli.main(["swap", "--config", "x.toml", "--timeout", "30"]) == 2
    assert time.monotonic() - t0 < 5
    assert "Nothing was prepared for removal" in capsys.readouterr().out
    assert not (c.data_path / CONTROL_DIR / SWAP_REQUEST).exists()


# ---------------------------------------------------------------------- 6. reconcile error on attach
def _two_copies_then_unplug(env: Env, io: ArchiveIO) -> tuple[ArchiveWorker, Bays, Any, list[Any]]:
    add_synthetic_run(env.ledger, env.cfg, 2, SEG)
    a = make_drive(env.tmp, "A")
    bays = Bays(a)
    w = env.worker(bays, io)
    drain(w)
    rows = env.ledger.query("SELECT copy_id, path FROM archive_copies WHERE kind='original' ORDER BY copy_id")
    assert len(rows) == 2
    bays.targets = []                                  # unclean unplug
    w.step()
    w.step()
    assert w.att is None
    return w, bays, a, rows


def test_an_error_during_reconcile_leaves_the_drive_unattached_and_reconcile_runs_again(env: Env) -> None:
    w, bays, a, rows = _two_copies_then_unplug(env, CappedIO({}))
    gone = Path(a.root) / rows[1]["path"]
    gone.chmod(0o666)
    gone.unlink()                                      # one copy missing when the drive comes back
    real = w.al.mark_lost                              # type: ignore[union-attr]
    calls = []

    def locked(*args: Any, **kw: Any) -> None:
        calls.append(args)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        real(*args, **kw)

    w.al.mark_lost = locked                            # type: ignore[union-attr,method-assign]
    bays.targets = [a]
    with pytest.raises(sqlite3.OperationalError):
        w.step()
    assert w.att is None                               # not attached without its reconcile
    add_synthetic_run(env.ledger, env.cfg, 1, SEG)
    drain(w)
    assert w.att is not None and len(calls) == 2
    states = {r["copy_id"]: r["state"] for r in env.ledger.query("SELECT copy_id, state FROM archive_copies")}
    assert states[rows[1]["copy_id"]] == "LOST" and states[rows[0]["copy_id"]] == "VERIFIED"
    check_drive(Path(a.root), env.ledger, w.att.drive_id, complete=False)


def test_an_unreadable_partial_is_not_restored_and_does_not_stop_the_reconcile(env: Env) -> None:
    class BadPartial(CappedIO):
        def read_back(self, path: Path):  # noqa: ANN201
            if path.name.endswith(".ts" + PARTIAL):
                raise OSError(errno.EIO, "simulated bad sector")
            return super().read_back(path)

    w, bays, a, rows = _two_copies_then_unplug(env, BadPartial({}))
    root = Path(a.root)
    p1, p2 = root / rows[0]["path"], root / rows[1]["path"]
    p1.chmod(0o666)
    p1.rename(p1.with_name(p1.name + PARTIAL))         # a rename rolled back, and its .partial unreadable
    p2.chmod(0o666)
    p2.unlink()
    bays.targets = [a]
    out = drain(w)                                     # no exception: both copies LOST, copied again
    assert out[-1] == "idle"
    assert not list(root.rglob("*" + PARTIAL))
    lost = env.ledger.query("SELECT copy_id FROM archive_copies WHERE state='LOST' ORDER BY copy_id")
    assert [r["copy_id"] for r in lost] == [rows[0]["copy_id"], rows[1]["copy_id"]]
    assert p1.is_file() and p2.is_file()               # copied again
    check_drive(root, env.ledger, w.att.drive_id, complete=False)   # type: ignore[union-attr]


# ---------------------------------------------------------------------- 7. Return to service, idempotent
def test_files_return_could_not_delete_are_deleted_later_and_the_office_check_passes(one,  # noqa: ANN001
                                                                                     monkeypatch) -> None:  # noqa: ANN001
    cfg, led, drive, w = one
    add_synthetic_run(led, cfg, 3, SEG, sidecars=True)
    drain(w)
    root = Path(drive.root)
    _to_office_and_back(w, drive)
    locked = _videos(root)[1]
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == locked:
            raise PermissionError(errno.EACCES, "The file is open in another program", str(self))
        real_unlink(self, missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)        # a video from the drive is open in a player
    _request(cfg, drive)
    add_synthetic_run(led, cfg, 1, SEG)
    w.step()                                           # returned; attached again (the file is still open)
    res = _result(cfg)
    assert res["ok"] and res["not_deleted"] == 1 and "deletes them by itself" in res["message"], res
    assert locked.is_file() and w.att is not None
    drain(w)
    monkeypatch.setattr(Path, "unlink", real_unlink)   # the player is closed
    _swap(w)                                           # deleted before the swap index is written
    assert not locked.exists()
    assert _office_check(root) == []
    check_drive(root, led, drive.drive_key, complete=True)
    assert _states(led) == {"TRANSFERRED": 6, "VERIFIED": 1}            # the new recording was not touched
    cleared = [json.loads(r["detail"]) for r in _events(led, "archive_drive_cleared")]
    assert any(d.get("leftovers") and d["deleted"] == 1 for d in cleared)


def test_leftovers_are_deleted_on_the_next_attach_never_a_verified_copy(one, monkeypatch) -> None:  # noqa: ANN001
    cfg, led, drive, w = one
    add_synthetic_run(led, cfg, 2, SEG)
    drain(w)
    root = Path(drive.root)
    _to_office_and_back(w, drive)
    locked = _videos(root)[0]
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == locked:
            raise PermissionError(errno.EACCES, "in use", str(self))
        real_unlink(self, missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    _request(cfg, drive)
    add_synthetic_run(led, cfg, 1, SEG)
    w.step()
    assert _result(cfg)["ok"] and locked.is_file()
    drain(w)
    new = [p for p in _videos(root) if p != locked]
    assert len(new) == 1
    monkeypatch.setattr(Path, "unlink", real_unlink)
    w2 = _worker(cfg, led, OneBay(drive))              # restart of the copier: the next attach cleans up
    w2.step()
    assert w2.att is not None and not locked.exists() and new[0].is_file()
    assert _states(led) == {"TRANSFERRED": 2, "VERIFIED": 1}
    check_drive(root, led, drive.drive_key, complete=True)


@pytest.mark.parametrize("finish", ["next_attach", "return_again"])
def test_a_return_interrupted_after_the_transfer_was_recorded_is_finished(one, finish: str) -> None:  # noqa: ANN001
    cfg, led, drive, w = one
    add_synthetic_run(led, cfg, 3, SEG, sidecars=True)
    drain(w)
    root = Path(drive.root)
    _to_office_and_back(w, drive)

    def power_cut(r: Path, paths: list[str], whole: bool = True) -> tuple[int, list[str]]:
        p = r / paths[0]                               # half cleared: one file deleted, then the power goes
        p.chmod(0o666)
        p.unlink()
        raise SystemExit("power cut")

    w._clear_drive = power_cut                         # type: ignore[method-assign]
    _request(cfg, drive)
    with pytest.raises(SystemExit):
        w.step()
    assert _states(led) == {"TRANSFERRED": 6}          # the transfer was recorded ...
    left = [p for p in (root / layout.ROOT).rglob("*") if p.suffix == ".ts" or p.name.endswith(".framehash.txt")]
    assert len(left) == 5                              # ... the drive half cleared, still waiting for its return
    w2 = _worker(cfg, led, OneBay(drive))              # the laptop restarts
    assert drive.drive_key in w2._awaiting
    if finish == "return_again":
        _request(cfg, drive, request_id="r-2")
    add_synthetic_run(led, cfg, 1, SEG)
    w2.step()
    if finish == "return_again":
        res = _result(cfg)
        assert res["ok"] and res["request_id"] == "r-2" and "has now been finished" in res["message"], res
    assert drive.drive_key not in w2._awaiting
    drain(w2)
    assert w2.att is not None and w2.att.drive_id == drive.drive_key   # in service again
    assert len(_videos(root)) == 1                     # only the new recording
    assert not list((root / layout.ROOT).rglob("*" + layout.FRAMEHASH_SUFFIX))
    assert _states(led) == {"TRANSFERRED": 6, "VERIFIED": 1}
    _swap(w2)                                          # next trip to the office: its check passes
    check_drive(root, led, drive.drive_key, complete=True)
    assert _office_check(root) == []
    cleared = [json.loads(r["detail"]) for r in _events(led, "archive_drive_cleared")]
    assert any(d.get("resumed") for d in cleared)
    assert len(_events(led, "archive_drive_returned")) == 1             # recorded once


def test_a_finished_return_is_not_repeated_and_a_drive_not_swapped_again_is_refused(one) -> None:  # noqa: ANN001
    """Idempotent: a second Return without a new swap still refuses (and deletes nothing in use)."""
    cfg, led, drive, w = one
    add_synthetic_run(led, cfg, 1, SEG)
    drain(w)
    _to_office_and_back(w, drive)
    _request(cfg, drive)
    add_synthetic_run(led, cfg, 1, SEG)
    w.step()
    assert _result(cfg)["ok"]
    drain(w)
    w.att = None                                       # (the in-use refusal is tested elsewhere)
    _request(cfg, drive, request_id="r-2")
    w._poll_return(w.locator.scan())
    res = _result(cfg)
    assert not res["ok"] and "not taken out with Swap and safely remove" in res["message"]
    assert _states(led) == {"TRANSFERRED": 1, "VERIFIED": 1} and len(_videos(Path(drive.root))) == 1
