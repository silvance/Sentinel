"""Start a new recording log (0.3.27): the supported way to clear test recordings before real use.

Owner's report 2026-11-20: the test recordings had been deleted from the storage folder by hand, because there
was no other way; the log still listed them and the recorder showed V-001, A-006 and A-008 for good.

The recording log, the archive copier and the drive (a folder) are the real ones. The recorder's control is a
stand-in, as is the list of drives that are plugged in."""

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
from tools.archive_bench import add_synthetic_run

from sentinel import newlog
from sentinel.archive import layout
from sentinel.archive.status import STATUS_FILE, read_archive_status
from sentinel.archive.worker import ArchiveWorker
from sentinel.db import Ledger

MIB = 1024 * 1024


class Control:
    """The recorder's control, standing in: remembers what it was asked."""

    name = "test"

    def __init__(self, running: bool = True) -> None:
        self.running, self.calls = running, []

    def is_running(self) -> bool:
        return self.running

    def stop(self, timeout_s: float = 90.0) -> bool:
        self.calls.append("stop")
        self.running = False
        return True

    def start(self) -> Any:
        self.calls.append("start")
        self.running = True
        return SimpleNamespace(ok=True, code=None, detail="started (test)")


class Site:
    """A recorder that has been tested: three recordings in its log, copied to one archive drive."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.cfg = make_config(tmp, archive={
            "enabled": True, "drive_path": str(tmp / "unused"), "require_drive_marker": False,
            "reserve_free_mb": 0.0, "poll_interval_s": 0.05, "chunk_mib": 1, "fsync_every_mib": 2,
            "max_rate_mb_s": 1000.0, "resume_policy": "any_drive"})
        self.db = self.cfg.data_path / "sentinel.db"
        self.drive = make_drive(tmp, "A", marker=True)
        self.locator = SwitchLocator(self.drive)
        self.control = Control()
        led = Ledger(self.db)
        try:
            self.ids = add_synthetic_run(led, self.cfg, 3, MIB)
            led.add_event("audit", "drives", "drive_prepared", "Drive prepared as SSD_1",
                          {"label": "SSD_1", "drive_uuid": self.drive.identity["drive_uuid"]})
            led.add_event("audit", "drives", "drive_prepared", "Drive prepared as SSD_2",
                          {"label": "SSD_2", "drive_uuid": "another"})
            self.copy(led)
            self.old_head = led.chain_head()
        finally:
            led.close()

    def copy(self, led: Ledger) -> ArchiveWorker:
        w = ArchiveWorker(self.cfg, ledger=led, locator=self.locator, allow_same_volume=True,
                          lower_own_priority=False)
        assert drain(w)[-1] == "idle"
        return w

    def spool_files(self) -> list[Path]:
        return [p for p in self.cfg.spool_path.rglob("*") if p.is_file()]

    def drive_files(self) -> list[str]:
        root = Path(self.drive.root)
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())

    def new_log(self, **kw: Any) -> newlog.Result:
        return newlog.run(self.tmp / "sentinel.toml", self.cfg, who="J. Tester", reason="testing finished",
                          control=self.control, locator=self.locator, same_volume_ok=True, say=lambda _s: None,
                          sleep=lambda _s: None, **kw)

    def events(self, db: Path, code: str) -> list[dict[str, Any]]:
        led = Ledger(db, readonly=True)
        try:
            return [json.loads(r[0]) for r in led.query("SELECT detail FROM events WHERE code=?", (code,))]
        finally:
            led.close()


@pytest.fixture()
def site(tmp_path: Path) -> Site:
    return Site(tmp_path)


def test_a_new_recording_log_sets_the_old_one_aside_and_clears_the_test_recordings(site: Site) -> None:
    assert len(site.spool_files()) >= 3
    on_drive = site.drive_files()
    assert any(f.startswith("Sentinel/") and f.endswith(".ts") for f in on_drive) and layout.START_HERE in on_drive
    assert any(os.stat(p).st_mode & stat.S_IWRITE == 0 for p in site.spool_files())    # sealed read-only
    plan = newlog.survey(site.cfg, locator=site.locator, same_volume_ok=True)
    assert plan.log_exists and plan.recordings == 3 and plan.spool_files == len(site.spool_files())
    assert [d.label for d in plan.drives] == ["ARCHIVE_A"] and plan.drives[0].files >= 3
    assert plan.drive_names == ["SSD_1", "SSD_2"]
    old_size = site.db.stat().st_size
    (site.cfg.data_path / "diagnostics").mkdir()
    (site.cfg.data_path / "diagnostics" / "report.zip").write_bytes(b"zip")

    res = site.new_log()

    # 1. The earlier log is kept, whole, with one last entry saying what happened to it.
    aside = Path(res.set_aside)
    assert aside.parent == site.cfg.data_path / newlog.SET_ASIDE_DIR and (aside / "sentinel.db").stat().st_size >= old_size
    led = Ledger(aside / "sentinel.db", readonly=True)
    try:
        assert led.query("SELECT COUNT(*) FROM segments")[0][0] == 3
        closed_head = led.chain_head()
    finally:
        led.close()
    closed = site.events(aside / "sentinel.db", "recording_log_closed")
    assert len(closed) == 1 and closed[0]["who"] == "J. Tester" and closed[0]["set_aside_to"] == str(aside)
    assert closed_head.seq > site.old_head.seq
    assert "Do not delete this folder" in (aside / "ABOUT THIS FOLDER.txt").read_text("utf-8")
    assert (aside / "diagnostics" / "report.zip").is_file() and (aside / STATUS_FILE).is_file()
    assert not (site.cfg.data_path / "diagnostics").exists() and not (site.cfg.data_path / STATUS_FILE).exists()
    # 2. The recordings are gone from this computer and from the drive; the drive is still a set-up drive.
    assert site.spool_files() == [] and site.cfg.spool_path.is_dir()
    assert site.drive_files() == ["SENTINEL_DRIVE.json"]
    assert res.not_removed == [] and res.drives[0].not_removed == [] and res.left_in_place == []
    # 3. The new log is empty and says where it comes from.
    led = Ledger(site.db, readonly=True)
    try:
        assert led.query("SELECT COUNT(*) FROM segments")[0][0] == 0
        assert led.query("SELECT COUNT(*) FROM archive_copies")[0][0] == 0
    finally:
        led.close()
    first = site.events(site.db, "recording_log_started")
    assert len(first) == 1 and first[0]["who"] == "J. Tester" and first[0]["reason"] == "testing finished"
    prev = first[0]["previous_log"]
    assert prev["recordings"] == 3 and prev["set_aside_to"] == str(aside)
    assert prev["chain_hash"] == closed_head.hash and prev["chain_seq"] == closed_head.seq
    assert first[0]["removed_from_this_computer"]["files"] == plan.spool_files
    assert first[0]["archive_drives_emptied"][0]["label"] == "ARCHIVE_A"
    # 4. Recording was stopped first and started again afterwards.
    assert site.control.calls == ["stop", "start"] and res.started is True


def test_the_cleared_recorder_and_drive_work_as_new(site: Site) -> None:
    """What the report showed must be gone: nothing missing, nothing unverified, nothing waiting to be copied;
    and the emptied drive takes new recordings."""
    site.new_log()
    led = Ledger(site.db)
    try:
        w = site.copy(led)                                 # attaches the emptied drive
        st = read_archive_status(site.cfg.data_path / STATUS_FILE)
        assert st["state"] == "UP_TO_DATE" and st["alerts"] == [] and st["backlog"]["segments"] == 0
        add_synthetic_run(led, site.cfg, 2, MIB)
        assert drain(w)[-1] == "idle"
        st = read_archive_status(site.cfg.data_path / STATUS_FILE)
        assert st["state"] == "UP_TO_DATE" and st["alerts"] == []
        assert led.query("SELECT COUNT(*) FROM archive_copies WHERE state='VERIFIED'")[0][0] >= 2
    finally:
        led.close()
    assert sum(1 for f in site.drive_files() if f.endswith(".ts")) == 2


def test_drive_names_stay_taken_in_the_new_log(site: Site) -> None:
    from sentinel.drives.policy import DrivePolicy
    from sentinel.drives.prepare import next_label

    site.new_log()
    led = Ledger(site.db)
    try:
        assert next_label(DrivePolicy(), led, []) == "SSD_3"       # not SSD_1 a second time
    finally:
        led.close()
    # ...and a second new log carries them on.
    site.new_log()
    assert newlog.survey(site.cfg, locator=site.locator, same_volume_ok=True).drive_names == ["SSD_1", "SSD_2"]


def test_a_log_that_is_open_elsewhere_is_not_replaced_and_nothing_is_deleted(site: Site) -> None:
    """A Sentinel window that is still open has the log open. Then nothing may be removed."""
    spool, drive = site.spool_files(), site.drive_files()
    other = Ledger(site.db, readonly=True)                 # the open window
    try:
        other.query("SELECT COUNT(*) FROM segments")
        with pytest.raises(newlog.NewLogRefused, match="Close every Sentinel window.*recording was started again"):
            site.new_log()
    finally:
        other.close()
    assert site.control.calls == ["stop", "start"] and site.control.running     # never left stopped
    assert site.spool_files() == spool and site.drive_files() == drive
    assert not (site.cfg.data_path / newlog.SET_ASIDE_DIR).exists()
    led = Ledger(site.db, readonly=True)
    try:
        assert led.query("SELECT COUNT(*) FROM segments")[0][0] == 3
    finally:
        led.close()
    assert len(site.events(site.db, "recording_log_closed")) == 1
    assert len(site.events(site.db, "recording_log_close_abandoned")) == 1
    # Once the window is closed it goes through.
    assert site.new_log().set_aside and site.spool_files() == []


def test_drives_can_be_left_alone(site: Site) -> None:
    drive = site.drive_files()
    res = site.new_log(empty_drives=False)
    assert res.drives == [] and site.drive_files() == drive and site.spool_files() == []


def test_a_file_that_cannot_be_deleted_is_named_and_does_not_stop_the_rest(site: Site, monkeypatch) -> None:
    victim = site.spool_files()[0]
    real = os.unlink

    def unlink(p: Any, *a: Any, **k: Any) -> None:
        if Path(p) == victim:
            raise PermissionError(13, "in use")
        real(p, *a, **k)

    monkeypatch.setattr(newlog.os, "unlink", unlink)
    res = site.new_log()
    assert res.not_removed == [str(victim)] and site.spool_files() == [victim]
    first = site.events(site.db, "recording_log_started")[0]
    assert first["removed_from_this_computer"]["not_removed"] == [str(victim)]


def test_a_drive_that_cannot_be_emptied_does_not_keep_the_new_log_from_starting(site: Site, monkeypatch) -> None:
    """Once the earlier log is set aside, the new one must get its first entry whatever happens to a drive."""
    real = newlog._remove

    def remove(path: Path) -> list[str]:
        if str(path).startswith(str(site.drive.root)):
            raise RuntimeError("the drive went away")
        return real(path)

    monkeypatch.setattr(newlog, "_remove", remove)
    res = site.new_log()
    assert res.set_aside and site.spool_files() == [] and res.started is True
    first = site.events(site.db, "recording_log_started")[0]
    assert "the drive went away" in first["archive_drives_emptied"][0]["not_removed"][0]


def _main(site: Site, monkeypatch, capsys, **kw: Any) -> tuple[int, str]:
    monkeypatch.setattr("sentinel.config.load_config", lambda _p: site.cfg)
    cfg_file = site.tmp / "sentinel.toml"
    cfg_file.write_text("# stand-in", encoding="utf-8")
    monkeypatch.setattr(newlog, "survey", lambda cfg, **k: _survey(cfg, locator=site.locator,
                                                                  empty_drives=k.get("empty_drives", True),
                                                                  same_volume_ok=True))
    code = newlog.main(cfg_file, control=site.control, locator=site.locator, **kw)
    return code, capsys.readouterr().out


_survey = newlog.survey


def test_the_command_says_what_it_will_do_and_needs_an_administrator_and_the_words(site: Site, monkeypatch,
                                                                                    capsys) -> None:
    spool = site.spool_files()
    code, out = _main(site, monkeypatch, capsys, dry_run=True)
    assert code == newlog.EXIT_OK and "DRY RUN: nothing was changed." in out
    assert "lists 3 recording(s)" in out and "SET ASIDE" in out and "ARCHIVE_A" in out and "NOT changed: the settings" in out
    code, out = _main(site, monkeypatch, capsys, admin=lambda: False, interactive=True, reader=lambda _p: "NEW LOG")
    assert code == newlog.EXIT_REFUSED and "administrator rights" in out and site.spool_files() == spool
    answers = iter(["yes"])
    code, out = _main(site, monkeypatch, capsys, admin=lambda: True, interactive=True, reader=lambda _p: next(answers))
    assert code == newlog.EXIT_REFUSED and "Not confirmed. Nothing was changed." in out and site.spool_files() == spool
    code, out = _main(site, monkeypatch, capsys, admin=lambda: True, interactive=False)      # nobody to ask
    assert code == newlog.EXIT_REFUSED and site.spool_files() == spool and site.control.calls == []


def test_the_command_does_it_when_confirmed(site: Site, monkeypatch, capsys) -> None:
    monkeypatch.setattr(newlog, "run", lambda *a, **k: _run(*a, **{**k, "same_volume_ok": True, "sleep": lambda _s: None}))
    answers = iter(["new log", "A. Agent"])
    code, out = _main(site, monkeypatch, capsys, admin=lambda: True, interactive=True, reader=lambda _p: next(answers))
    assert code == newlog.EXIT_OK, out
    assert "DONE: a new recording log was started." in out and "Recording was started again." in out
    assert "Archive drive ARCHIVE_A emptied" in out and site.spool_files() == []
    assert site.events(site.db, "recording_log_started")[0]["who"] == "A. Agent"


_run = newlog.run
