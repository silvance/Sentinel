"""Return an archive drive to service (0.3.9, owner request 2026-11-12): the office copies a full archive drive
to its own storage and checks it; an administrator then returns the drive to service on the laptop. The copies
are recorded as TRANSFERRED (still archived, at the place named), only then deleted from the drive, and the
drive is used again with its marker and label kept."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from archive_helpers import drain, make_drive
from conftest import make_config
from sentinel.archive import layout
from sentinel.archive import __main__ as arc_cli
from sentinel.archive.io import ArchiveIO
from sentinel.archive.ledger import ArchiveLedger
from sentinel.archive.locator import DriveScan
from sentinel.archive.worker import CONTROL_DIR, RETURN_REQUEST, RETURN_RESULT, ArchiveWorker
from sentinel.clock import iso_utc
from sentinel.db import Ledger
from sentinel.ledgerchain import verify_ledger
from sentinel.retention.manager import RetentionManager
from tools.archive_bench import add_synthetic_run

SEG = 200_000


class OneBay:
    def __init__(self, target: Any) -> None:
        self.target = target

    def scan(self) -> DriveScan:
        return DriveScan([self.target] if Path(self.target.root).is_dir() else [])


@pytest.fixture()
def env(tmp_path: Path):  # noqa: ANN201
    cfg = make_config(tmp_path, archive={
        "enabled": True, "drive_path": str(tmp_path / "unused"), "require_drive_marker": False,
        "reserve_free_mb": 0.0, "poll_interval_s": 0.05, "chunk_mib": 1, "fsync_every_mib": 2,
        "max_rate_mb_s": 1000.0, "resume_policy": "this_drive"})
    led = Ledger(cfg.data_path / "sentinel.db")
    drive = make_drive(tmp_path, "A", marker=True)
    w = ArchiveWorker(cfg, ledger=led, locator=OneBay(drive), io=ArchiveIO(read_chunk=1024 * 1024),
                      allow_same_volume=True, lower_own_priority=False)
    yield cfg, led, drive, w
    led.close()


def _request(cfg: Any, drive: Any, **over: Any) -> str:
    req = {"written_utc": iso_utc(time.time()), "request_id": "r-1", "drive_uuid": drive.identity["drive_uuid"],
           "admin_id": "tech1", "destination": "Office archive drive 3", "copied_by": "Clerk A",
           "copied_on": "2026-11-26", "checked": True, **over}
    d = cfg.data_path / CONTROL_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / RETURN_REQUEST).write_text(json.dumps(req), "utf-8")
    return req["request_id"]


def _result(cfg: Any) -> dict[str, Any]:
    return json.loads((cfg.data_path / CONTROL_DIR / RETURN_RESULT).read_text("utf-8"))


def _to_office_and_back(w: ArchiveWorker, drive: Any) -> None:
    """Swap archive drive, take the drive out (two scans without it), plug it back in after the office copy."""
    w.request_swap()
    for _ in range(20):
        if w.step() == "swapped":
            break
    else:
        raise AssertionError("the swap did not finish")
    root = Path(drive.root)
    away = root.with_name(root.name + "_at_office")
    root.rename(away)
    w.step()
    w.step()
    away.rename(root)


def _videos(root: Path) -> list[Path]:
    return sorted(p for p in (root / layout.ROOT).rglob("*.ts"))


def _states(led: Ledger) -> dict[str, int]:
    return {r["state"]: r["n"] for r in led.query("SELECT state, COUNT(*) n FROM archive_copies GROUP BY state")}


def test_a_returned_drive_is_recorded_cleared_and_used_again(env) -> None:  # noqa: ANN001
    cfg, led, drive, w = env
    add_synthetic_run(led, cfg, 3, SEG, sidecars=True)
    drain(w)
    root = Path(drive.root)
    assert len(_videos(root)) == 3 and _states(led) == {"VERIFIED": 6}
    _to_office_and_back(w, drive)
    add_synthetic_run(led, cfg, 1, SEG)
    assert w.step() == "absent" and w.att is None   # back from the office: not written to before its return
    assert len(_videos(root)) == 3

    _request(cfg, drive)
    assert w.step() == "copied"                  # returned, then in use again at once (the new recording)
    res = _result(cfg)
    assert res["ok"] and res["request_id"] == "r-1" and res["files"] == 6, res
    assert "Office archive drive 3" in res["message"]
    old_names = {p.name for p in _videos(root)}
    assert len(old_names) == 1 and not list((root / layout.ROOT).rglob("*.framehash.txt"))   # only the new one
    assert (root / "SENTINEL_DRIVE.json").is_file()                   # marker (identity, label) kept
    assert _states(led) == {"TRANSFERRED": 6, "VERIFIED": 1}
    ev = led.query("SELECT detail FROM events WHERE code='archive_drive_returned'")
    detail = json.loads(ev[0]["detail"])
    assert detail["destination"] == "Office archive drive 3" and detail["copied_by"] == "Clerk A"
    assert detail["admin_id"] == "tech1" and detail["checked"] is True and detail["copies"] == 6
    al = ArchiveLedger(led, "CAM01", "this_drive")
    assert al.backlog().segments == 0 and al.open_problems() == []    # still archived; no A-006
    assert verify_ledger(cfg.data_path / "sentinel.db").ok            # the change is allowed by the chain

    # Copying carries on to the same drive with new recordings only.
    add_synthetic_run(led, cfg, 1, SEG)
    drain(w)
    assert len(_videos(root)) == 2 and _states(led) == {"TRANSFERRED": 6, "VERIFIED": 2}
    w.step()                                     # detach/attach again: transferred copies are not "lost"
    assert al.open_problems() == []


def test_nothing_is_deleted_when_a_recording_is_missing_from_the_drive(env) -> None:  # noqa: ANN001
    cfg, led, drive, w = env
    add_synthetic_run(led, cfg, 2, SEG)
    drain(w)
    _to_office_and_back(w, drive)
    gone = _videos(Path(drive.root))[0]
    gone.chmod(0o666)
    gone.unlink()
    _request(cfg, drive)
    w.step()
    res = _result(cfg)
    assert not res["ok"] and "missing or changed" in res["message"] and "Nothing was deleted" in res["message"]
    assert len(_videos(Path(drive.root))) == 1 and "TRANSFERRED" not in _states(led)


@pytest.mark.parametrize("over", [{"checked": False}, {"destination": " "}, {"copied_by": ""},
                                  {"drive_uuid": "00000000-0000-0000-0000-000000000000"},
                                  {"written_utc": "2020-01-01T00:00:00.000Z"}])
def test_an_incomplete_stale_or_wrong_request_deletes_nothing(env, over) -> None:  # noqa: ANN001
    cfg, led, drive, w = env
    add_synthetic_run(led, cfg, 2, SEG)
    drain(w)
    _request(cfg, drive, **over)
    w.step()
    assert not _result(cfg)["ok"]
    assert len(_videos(Path(drive.root))) == 2 and _states(led) == {"VERIFIED": 2}
    assert not (cfg.data_path / CONTROL_DIR / RETURN_REQUEST).exists()      # consumed, never retried


def test_retention_counts_a_transferred_copy_as_off_this_computer(env) -> None:  # noqa: ANN001
    cfg, led, drive, w = env
    add_synthetic_run(led, cfg, 1, SEG)
    drain(w)
    seg = led.query("SELECT segment_id FROM segments")[0]["segment_id"]
    rm = RetentionManager(cfg, led)
    with led.tx() as c:
        assert rm._copies(c, seg, "original", eligible=set()) == []          # drive still here: not eligible
    _to_office_and_back(w, drive)
    _request(cfg, drive)
    w.step()
    with led.tx() as c:
        got = rm._copies(c, seg, "original", eligible=set())
    assert [x["state"] for x in got] == ["TRANSFERRED"]


def test_the_command_refuses_without_administrator_or_confirmation(env, monkeypatch, tmp_path: Path,
                                                                  capsys) -> None:  # noqa: ANN001
    cfg, led, drive, w = env
    monkeypatch.setattr(arc_cli, "_load", lambda _a: cfg)
    base = ["return", "--config", "x.toml", "--drive", str(drive.root), "--admin-id", "tech1",
            "--destination", "Office 3", "--copied-by", "Clerk A", "--copied-on", "2026-11-01"]
    import sentinel.drives.prepare as prep

    monkeypatch.setattr(prep, "os_administrator_check", lambda: (False, "test"))
    assert arc_cli.main([*base, "--checked"]) == 4
    monkeypatch.setattr(prep, "os_administrator_check", lambda: (True, "test"))
    assert arc_cli.main(base) == 1                                           # not confirmed
    assert arc_cli.main([*base[:-1], "2999-01-01", "--checked"]) == 1        # date in the future
    assert arc_cli.main([*base, "--checked"]) == 3                           # copier not running
    plain = tmp_path / "plain"
    plain.mkdir()
    assert arc_cli.main([*base[:4], str(plain), *base[5:], "--checked"]) == 2   # not an archive drive
    assert not (cfg.data_path / CONTROL_DIR / RETURN_REQUEST).exists()
    assert "Nothing was deleted" in capsys.readouterr().err


def test_a_drive_renamed_while_attached_is_shown_by_its_new_name(env) -> None:  # noqa: ANN001
    """0.3.11: Drives › Rename… while the drive is in use: same drive (same key), new name at once."""
    from sentinel.export.target import ExportTarget

    cfg, led, drive, w = env
    w.step()
    assert w.att is not None and w.att.target.label == "ARCHIVE_A"
    renamed = ExportTarget(drive.root, dict(drive.identity, label="SSD_1"))
    w.locator = OneBay(renamed)
    w.step()
    assert w.att.drive_id == renamed.drive_key and w.att.target.label == "SSD_1"
    assert led.query("SELECT label FROM drives WHERE drive_id=?", (renamed.drive_key,))[0][0] == "SSD_1"


# ---------------------------------------------------------------------- review 2026-11-12
def test_the_drive_in_use_or_one_never_taken_out_is_not_returned(env) -> None:  # noqa: ANN001
    cfg, led, drive, w = env
    add_synthetic_run(led, cfg, 2, SEG)
    drain(w)
    _request(cfg, drive)
    w.step()
    res = _result(cfg)
    assert not res["ok"] and "copying to now" in res["message"] and "Nothing was deleted" in res["message"]
    w.att = None                                 # say it is not in use: it was still never taken out with Swap
    _request(cfg, drive)
    w._poll_return(w.locator.scan())
    res = _result(cfg)
    assert not res["ok"] and "not taken out with Swap archive drive" in res["message"]
    assert _states(led) == {"VERIFIED": 2}


def test_recordings_copied_after_the_swap_are_never_recorded_as_at_the_office(env) -> None:  # noqa: ANN001
    """The reviewers' reproduction: swap, office copy, plug back in, copying resumes on it (here: Cancel swap,
    or a drive swapped before 0.3.12), then Return to service."""
    cfg, led, drive, w = env
    add_synthetic_run(led, cfg, 2, SEG)
    drain(w)
    _to_office_and_back(w, drive)
    w.resume()                                   # Cancel swap: keep using the drive as it is
    add_synthetic_run(led, cfg, 2, SEG)
    drain(w)
    assert len(_videos(Path(drive.root))) == 4
    _request(cfg, drive)
    w.att = None                                 # (the in-use refusal is tested above)
    w._poll_return(w.locator.scan())
    res = _result(cfg)
    assert not res["ok"] and "copied to ARCHIVE_A after it was taken out" in res["message"], res
    assert res["copied_after_swap"] == 2 and "TRANSFERRED" not in _states(led)
    assert len(_videos(Path(drive.root))) == 4


def test_a_drive_back_from_the_office_waits_for_its_return_even_after_a_restart(env) -> None:  # noqa: ANN001
    cfg, led, drive, w = env
    add_synthetic_run(led, cfg, 1, SEG)
    drain(w)
    _to_office_and_back(w, drive)
    add_synthetic_run(led, cfg, 1, SEG)
    w2 = ArchiveWorker(cfg, ledger=led, locator=OneBay(drive), io=ArchiveIO(read_chunk=1024 * 1024),
                       allow_same_volume=True, lower_own_priority=False)
    assert w2.step() == "absent" and w2.att is None
    st = json.loads((cfg.data_path / "archive_status.json").read_text("utf-8"))
    assert st["awaiting_return"][0]["label"] == "ARCHIVE_A"
    assert "returned to service" in st["message"] and "Cancel swap" in st["message"]
    assert {d["label"]: d["use"] for d in st["drives"]} == {"ARCHIVE_A": "awaiting_return"}
    w2.resume()                                  # Cancel swap: use it again without an office copy
    assert w2.step() in ("copied", "idle") and w2.att is not None


def test_one_bay_dock_office_cycle_never_locks_out_the_other_drive(env, tmp_path: Path) -> None:  # noqa: ANN001
    """First review run, 2026-11-12: docs/10 step 1 (one-bay dock) swaps the drive in use out only to make room
    for the drive coming back from the office; that drive must be used again afterwards."""
    cfg, led, ssd1, w = env
    ssd2 = make_drive(tmp_path, "B", marker=True)
    add_synthetic_run(led, cfg, 1, SEG)
    drain(w)                                         # SSD_1 in use
    _to_office_and_back(w, ssd1)                     # SSD_1 swapped out for the office ...
    w.locator = OneBay(ssd2)                         # ... SSD_2 goes into the bay meanwhile
    add_synthetic_run(led, cfg, 1, SEG)
    drain(w)
    assert w.att is not None and w.att.target.label == ssd2.label
    w.locator = OneBay(ssd1)                         # (the bay holds one drive: SSD_2 is out of the scan)
    w.locator = OneBay(ssd2)
    _to_office_and_back(w, ssd2)                     # step 1: Swap SSD_2 out to make room (not to the office)
    w.locator = OneBay(ssd1)                         # SSD_1 back from the office
    _request(cfg, ssd1)
    w.step()
    assert _result(cfg)["ok"], _result(cfg)
    w.request_swap()                                 # later SSD_1 is swapped again ...
    for _ in range(10):
        if w.step() == "swapped":
            break
    w.locator = OneBay(ssd2)                         # ... and SSD_2 goes back in: it is used again
    add_synthetic_run(led, cfg, 1, SEG)
    drain(w)
    assert w.att is not None and w.att.target.label == ssd2.label


def test_a_swapped_drive_still_plugged_in_is_not_returned(env) -> None:  # noqa: ANN001
    cfg, led, drive, w = env
    add_synthetic_run(led, cfg, 2, SEG)
    drain(w)
    w.request_swap()
    for _ in range(10):
        if w.step() == "swapped":
            break
    _request(cfg, drive)                             # never taken out: not at the office
    w.step()
    res = _result(cfg)
    assert not res["ok"] and "has not been taken out since" in res["message"]
    assert _states(led) == {"VERIFIED": 2}
