"""Safely remove… and archive drives (0.3.27).

The owner's report of 2026-11-20, from the laptop: Safely remove… pressed for the archive drive in use answered
"Error M-007: The drive could not be safely removed. Do not unplug the drive yet. Close any open files on it and
try again. …" with the real reason (press Swap and safely remove instead) on the line under it. The Drives page
no longer offers that button for a drive the copier is using (tests/unit/test_gui_app.py). Here: what the
command says and records, that Swap works in the state the laptop was in (some recordings cannot be read), and
the drive that came out with a swap and is plugged in again, which neither button took out.

The archive copier is the real one, with a folder as its drive; only Windows' own removal is a stand-in."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from archive_helpers import SwitchLocator, drain, make_drive
from conftest import make_config
from drive_fakes import FakeInspector, data_part, protected_for, system_disk, usb_disk, vol
from tools.archive_bench import add_synthetic_run

from sentinel.alerts import CATALOG, Severity, make_alert
from sentinel.archive.status import STATUS_FILE, read_archive_status
from sentinel.archive.worker import ArchiveWorker
from sentinel.db import Ledger
from sentinel.drives import cli
from sentinel.drives import eject as ej
from sentinel.drives.errors import SafeRemoveError
from sentinel.drives.marker import MARKER_NAME, RootContents
from sentinel.drives.policy import DrivePolicy
from sentinel.export.target import ManualRemovalFallback

MIB = 1024 * 1024


class World:
    """One archive drive (a folder), the real copier on it, and the ``drives eject`` command looking at the same
    data folder, as on the recorder."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.cfg = make_config(tmp, archive={
            "enabled": True, "drive_path": str(tmp / "unused"), "require_drive_marker": False,
            "reserve_free_mb": 0.0, "poll_interval_s": 0.05, "chunk_mib": 1, "fsync_every_mib": 2,
            "max_rate_mb_s": 1000.0, "resume_policy": "any_drive"})
        self.ledger = Ledger(self.cfg.data_path / "sentinel.db")
        self.drive = make_drive(tmp, "A")
        self.key = self.drive.drive_key
        self.locator = SwitchLocator(self.drive)
        # The deployed copier cannot switch a drive off by itself: the Drives page does it (drives eject).
        self.worker = ArchiveWorker(self.cfg, ledger=self.ledger, locator=self.locator, allow_same_volume=True,
                                    lower_own_priority=False, remover=ManualRemovalFallback())
        marker = {"format": "sentinel-drive", "version": 1, "drive_uuid": self.drive.identity["drive_uuid"],
                  "label": "SSD_1", "role": "archive"}
        v = vol("c0ffee00-0000-0000-0000-00000000bbbb", "ntfs", label="SSD_1", letter="E")
        self.disk = usb_disk(disk_id="4", partitions=(data_part(1, v),))
        self.env = SimpleNamespace(
            policy=DrivePolicy(erase_enabled=True), protected=protected_for(), windows=True,
            cfg=SimpleNamespace(data_path=self.cfg.data_path), enumerate=lambda: [self.disk, system_disk()],
            inspector=FakeInspector({v.volume_id: RootContents(
                readable=True, entries=(MARKER_NAME, "Sentinel"), marker=marker,
                sentinel_signs=("marker", "archive_layout"))}))

    def status(self) -> dict[str, Any]:
        doc = read_archive_status(self.cfg.data_path / STATUS_FILE)
        assert doc is not None
        return doc

    def eject(self) -> int:
        return cli.cmd_eject(self.env, cli.build_parser().parse_args(["eject", "--disk", "4"]))

    def events(self, code: str) -> list[dict[str, Any]]:
        return [json.loads(r["detail"]) for r in self.ledger.query("SELECT detail FROM events WHERE code=?", (code,))]

    def unplug(self) -> None:
        self.locator.target = None
        self.worker.step()
        self.worker.step()                 # gone for two scans in a row: really taken out

    def plug_in(self) -> str:
        self.locator.target = self.drive
        return self.worker.step()


@pytest.fixture()
def world(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path)
    yield w
    w.ledger.close()


@pytest.fixture()
def removals(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Windows' own removal, standing in: it works, and says which disk it was asked for."""
    seen: list[str] = []

    def fake_safe_remove(d, disks, protected, *, enumerate_fn):  # noqa: ANN001, ANN202
        seen.append(d.disk_id)
        return ej.SafeRemoveResult(True, "SAFE TO REMOVE DRIVE", needs_admin=False)

    monkeypatch.setattr(ej, "safe_remove", fake_safe_remove)
    return seen


def test_safely_remove_for_the_drive_in_use_says_which_button_and_is_recorded(world: World, removals: list[str],
                                                                               capsys) -> None:
    """What the owner pressed. Still refused (the copier may be writing to the drive), but the message no longer
    starts with advice about open files, and the recording log keeps what the person was told."""
    assert world.worker.step() == "idle" and world.status()["drives"][0]["use"] == "in_use"
    assert world.eject() == cli.EXIT_REFUSED and removals == []
    out = capsys.readouterr().out
    head, reason = out.strip().split("\n", 1)
    assert head.startswith("Error M-007: The drive could not be safely removed. Do not unplug the drive yet.")
    assert "open files" not in out
    assert reason.startswith("SSD_1 is an archive drive and archive copying is running, so it was not removed.")
    assert "press Swap and safely remove" in reason
    ev = world.events("drive_not_removed")
    assert len(ev) == 1 and ev[0]["reason"] == "archive_drive_in_use" and ev[0]["code"] == "M-007"
    assert ev[0]["label"] == "SSD_1" and ev[0]["drive_id"] == world.key and ev[0]["text"] == reason
    assert world.events("archive_swap_ready") == []


def test_swap_takes_the_drive_out_while_some_recordings_cannot_be_copied(world: World, removals: list[str],
                                                                          capsys) -> None:
    """The laptop on 2026-11-20: "Some recordings could not be copied to the archive drive", A-008. A swap does not
    wait for what cannot be copied: it prepares the drive, and the page's removal then switches it off."""
    ids = add_synthetic_run(world.ledger, world.cfg, 2, MIB)
    p = world.cfg.spool_path / world.ledger.get_segment(ids[0])["rel_path"]
    os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
    with open(p, "r+b") as fh:                        # a recording that no longer matches its sealed hash
        fh.write(b"X")
    assert drain(world.worker)[-1] == "idle"
    st = world.status()
    assert st["state"] == "ATTENTION" and st["message"].startswith("Some recordings could not be copied")
    assert {a["code"] for a in st["alerts"]} >= {"A-008"}
    world.worker.request_swap()
    assert world.worker.step() == "swapped"
    st = world.status()
    assert st["state"] == "REMOVE_MANUALLY" and st["removal"]["drive_id"] == world.key
    assert world.eject() == cli.EXIT_OK and removals == ["4"]
    assert capsys.readouterr().out.strip() == "SAFE TO REMOVE DRIVE"
    assert len(world.events("archive_swap_ready")) == 1 and world.events("drive_not_removed") == []


def test_a_drive_plugged_in_again_after_its_swap_can_be_safely_removed(world: World, removals: list[str],
                                                                       capsys) -> None:
    """Found while looking into the report: the drive came out with a swap, is plugged in again, and has to come
    out again before it is returned to service. Safely remove… refused it and pointed at Swap; Swap prepares
    nothing for a drive the copier does not use. Nothing was recorded here before the swap (no camera)."""
    w = world.worker
    assert w.step() == "idle"
    w.request_swap()
    assert w.step() == "swapped" and world.eject() == cli.EXIT_OK
    world.unplug()
    assert world.plug_in() == "absent"
    st = world.status()
    assert [a["drive_id"] for a in st["awaiting_return"]] == [world.key] and st["removal"] is None
    assert st["drives"][0]["use"] == "awaiting_return"
    w.request_swap()                                   # Swap: nothing to prepare for this drive
    assert w.step() == "absent" and world.status()["removal"] is None
    swaps = world.events("archive_swap_ready")
    capsys.readouterr()
    assert world.eject() == cli.EXIT_OK and removals == ["4", "4"]
    assert capsys.readouterr().out.strip() == "SAFE TO REMOVE DRIVE"
    # Its swap is already recorded: nothing a second time, and it still waits for its return to service.
    assert world.events("archive_swap_ready") == swaps and world.events("drive_not_removed") == []
    world.unplug()
    assert world.plug_in() == "absent" and world.status()["awaiting_return"]
    # Cancel swap puts it back in use: then it is the drive the copier writes to, and is refused as before.
    w.resume()
    assert w.step() == "idle" and world.status()["drives"][0]["use"] == "in_use"
    assert world.eject() == cli.EXIT_REFUSED and removals == ["4", "4"]


def test_another_archive_drive_than_the_ones_the_copier_released_is_still_refused(world: World,
                                                                                  removals: list[str]) -> None:
    assert world.worker.step() == "idle"
    doc = world.status()
    doc.update(awaiting_return=[{"drive_id": "drive_uuid:someone-else", "label": "SSD_2"}],
               removal={"drive_id": "drive_uuid:a-third-drive", "safe": False})
    (world.cfg.data_path / STATUS_FILE).write_text(json.dumps(doc), encoding="utf-8")
    assert world.eject() == cli.EXIT_REFUSED and removals == []
    # A list in a form this command does not know is not a release either.
    for odd in ("drive_uuid:x", [world.key], {"drive_id": world.key}, None):
        doc.update(awaiting_return=odd, removal=None)
        (world.cfg.data_path / STATUS_FILE).write_text(json.dumps(doc), encoding="utf-8")
        assert world.eject() == cli.EXIT_REFUSED and removals == []


def test_a_removal_windows_refuses_says_what_to_close_and_is_recorded(world: World, monkeypatch, capsys) -> None:
    """Copying stopped, so the command goes to Windows; something on the drive is open."""
    def veto(d, disks, protected, *, enumerate_fn):  # noqa: ANN001, ANN202
        cm = SimpleNamespace(locate=lambda i: 1, ancestors=lambda n, levels=16: [2], device_id=lambda n: f"node{n}",
                             status=lambda n: ej.DN_REMOVABLE if n == 2 else 0, eject=lambda n: (0, 6, "vlc.exe"))
        monkeypatch.setattr(ej, "_flush_windows", lambda disk, steps: None)
        monkeypatch.setattr(ej, "_CfgMgr", lambda: cm)
        return ej.safe_remove_windows(d, disks, protected, enumerate_fn=enumerate_fn, wait_s=0.1)

    monkeypatch.setattr(ej, "safe_remove", veto)
    world.env.enumerate = lambda: [world.disk]
    assert world.eject() == cli.EXIT_FAILED
    out = capsys.readouterr().out
    assert out.startswith("Error M-007: The drive could not be safely removed.")
    assert "a video playing from it, a folder window showing it" in out and "open files" not in out
    ev = world.events("drive_not_removed")
    assert len(ev) == 1 and ev[0]["reason"] == "refused_by_windows"
    assert ev[0]["detail"]["veto"] == "outstanding open (files in use)" and ev[0]["label"] == "SSD_1"


def test_a_removal_the_drive_itself_rules_out_is_recorded_with_its_reason(world: World, monkeypatch, capsys) -> None:
    def refuse(d, disks, protected, *, enumerate_fn):  # noqa: ANN001, ANN202
        raise SafeRemoveError("not_removable", "This drive cannot be removed safely while the computer is on.",
                              {"chain": ["node1", "node2"]})

    monkeypatch.setattr(ej, "safe_remove", refuse)
    assert world.eject() == cli.EXIT_REFUSED
    assert "This drive cannot be removed safely while the computer is on." in capsys.readouterr().out
    ev = world.events("drive_not_removed")
    assert ev[0]["reason"] == "not_removable" and ev[0]["detail"] == {"chain": ["node1", "node2"]}


def test_a_refusal_is_said_even_when_it_cannot_be_recorded(world: World, monkeypatch, capsys) -> None:
    """The record is for technical support; a recording log that cannot be written must not change the answer."""
    assert world.worker.step() == "idle"

    class Broken:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            raise OSError(5, "Access is denied")

    import sentinel.db as db

    monkeypatch.setattr(db, "Ledger", Broken)
    assert world.eject() == cli.EXIT_REFUSED
    assert "press Swap and safely remove" in capsys.readouterr().out


def test_m007_no_longer_gives_advice_that_fits_one_cause_only() -> None:
    d = CATALOG["M-007"]
    assert d.headline == "The drive could not be safely removed" and "open files" not in d.action
    assert d.action.startswith("Do not unplug the drive yet.")
    assert make_alert("M-007", Severity.WARNING).message.startswith(
        "Error M-007: The drive could not be safely removed. Do not unplug the drive yet.")
