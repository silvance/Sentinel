"""Regression tests for the medium (and two low) drive findings of the 2026-11-12 overnight review
(docs/review-2026-11-12-findings.json). No real disk is touched: enumeration, the elevated helper and
the device tree are fakes; PowerShell only evaluates a pure helper function with a stand-in cmdlet."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from drive_fakes import (
    FakeInspector,
    FakeRunner,
    Sequence,
    blank_disk,
    data_part,
    foreign_disk,
    load_fixture,
    prepared_disk_like,
    protected_for,
    raw_disk,
    system_disk,
    usb_disk,
    vol,
)

from sentinel.db import Ledger
from sentinel.drives import cli
from sentinel.drives import eject as ej
from sentinel.drives import rename as rn
from sentinel.drives.classify import UNKNOWN_CONTENTS_MESSAGE, classify, classify_all
from sentinel.drives.commands import WINDOWS_TEMPLATE, build_linux_script, windows_expected
from sentinel.drives.errors import PrepareFailed, PrepareRefused
from sentinel.drives.known import KnownDrives, load_known
from sentinel.drives.marker import MARKER_NAME, RootContents, write_marker
from sentinel.drives.model import DriveClass
from sentinel.drives.policy import DrivePolicy, load_policy
from sentinel.drives.prepare import (
    EXIT_ELEVATION_CANCELLED,
    Authorization,
    Confirmation,
    HelperResult,
    PrepareContext,
    check_guards,
    linux_helper_result,
    next_label,
    prepare_drive,
)
from sentinel.drives.windows_enum import parse_windows_snapshot

POL = DrivePolicy(erase_enabled=True)
SYS = system_disk()
ZEROS = "000000000000000000"
ADMIN = Authorization("admin1", administrator=True, method="test")
OPERATOR = Authorization("operator7", administrator=False, method="test")
UID = "0b0e5f0a-1c2d-4e3f-8a9b-0c1d2e3f4a5b"
ARCHIVE_MARKER = {"format": "sentinel-drive", "version": 1, "drive_uuid": UID, "label": "SSD_1",
                  "prepared_utc": "2026-11-11T00:00:00Z", "prepared_by": "tech", "role": "archive"}


@pytest.fixture()
def ledger(tmp_path: Path):
    led = Ledger(tmp_path / "data" / "sentinel.db")
    yield led
    led.close()


@pytest.fixture()
def no_sleep(monkeypatch):
    """The ledger-lock retries sleep up to 8 s each; not in tests."""
    import sentinel.export.ledger as xl

    monkeypatch.setattr(xl.time, "sleep", lambda _s: None)


class Locked:
    """A ledger whose writes of the given event codes fail with 'database is locked' (the recorder or the
    archive copier holding the write lock)."""

    def __init__(self, led: Ledger, *codes: str) -> None:
        self.led, self.codes, self.attempts = led, set(codes), 0

    def query(self, *a, **k):
        return self.led.query(*a, **k)

    def add_event(self, severity, category, code, message, detail=None):
        if code in self.codes:
            self.attempts += 1
            raise sqlite3.OperationalError("database is locked")
        return self.led.add_event(severity, category, code, message, detail)


def _events(led: Ledger, code: str) -> list[dict]:
    return [json.loads(r["detail"]) for r in led.query("SELECT detail FROM events WHERE code=?", (code,))]


def _ready_archive(disk_id: str = "4", **kw):
    v = vol("c0ffee00-0000-0000-0000-00000000bbbb", "ntfs", label="SSD_1", letter="E")
    d = usb_disk(disk_id=disk_id, partitions=(data_part(1, v),), **kw)
    rc = RootContents(readable=True, entries=(MARKER_NAME, "Sentinel"), marker=ARCHIVE_MARKER,
                      sentinel_signs=("marker", "archive_layout"))
    return d, FakeInspector({v.volume_id: rc})


# ============================================================================ 1. operator window, no ledger access

def test_without_administrator_rights_the_archive_drive_still_shows_as_ready() -> None:
    arc, insp = _ready_archive()
    known = KnownDrives(error="access denied", needs_admin=True)
    classes, problems = classify_all([arc, blank_disk(), SYS], POL, protected_for(), inspector=insp, known=known)
    by = {c.disk.disk_id: c for c in classes}
    assert by["4"].drive_class is DriveClass.READY and not by["4"].can_prepare
    assert "technical support" not in by["4"].message
    # Other drives cannot be checked, and say who can check them (not "Contact technical support").
    assert by["9"].drive_class is DriveClass.UNSUITABLE and by["9"].reasons == ("ledger_needs_admin",)
    assert "administrator" in by["9"].message and "technical support" not in by["9"].message
    assert not by["9"].can_prepare and problems


def test_a_broken_ledger_still_fails_closed_and_protection_problems_still_block_ready() -> None:
    arc, insp = _ready_archive()
    c = classify(arc, POL, protected_for(), inspector=insp, known=KnownDrives(error="disk I/O error"))
    assert c.drive_class is DriveClass.READY       # READY never offers erasing
    b = classify(blank_disk(), POL, protected_for(), inspector=insp, known=KnownDrives(error="disk I/O error"))
    assert b.drive_class is DriveClass.UNSUITABLE and b.reasons == ("ledger_unavailable",) and not b.can_prepare
    # An unresolved protected-storage check still keeps the drive from READY (it could be made the archive).
    p = classify(arc, POL, protected_for(), inspector=insp, protection_ok=False)
    assert p.drive_class is not DriveClass.READY and not p.can_prepare


def test_access_denied_ledger_is_reported_as_needing_an_administrator(tmp_path: Path, monkeypatch) -> None:
    env = cli._Env.__new__(cli._Env)
    env.cfg = SimpleNamespace(data_path=tmp_path)
    monkeypatch.setattr(cli._installed, "path_state", lambda p: (None, "[WinError 5] Access is denied"))
    k = env.known()
    assert k.error and k.needs_admin and "administrator" in k.error


# ============================================================================ 2. RAW disk on Windows

def test_a_raw_windows_disk_is_not_called_empty() -> None:
    c = classify(raw_disk(), POL, protected_for(), inspector=FakeInspector())
    assert c.drive_class is DriveClass.FOREIGN_DATA and c.reasons == ("unknown_contents",)
    assert c.message == UNKNOWN_CONTENTS_MESSAGE and "empty and must be set up" not in c.message
    assert c.can_prepare and c.needs_admin and c.needs_serial          # default policy admin_only
    never = classify(raw_disk(), load_policy({"erase_enabled": True, "foreign_data_authorization": "never"}),
                     protected_for(), inspector=FakeInspector())
    assert not never.can_prepare and never.refusal == "policy_never"
    with pytest.raises(PrepareRefused) as ei:
        check_guards(c, Confirmation.for_classification(c, "ABCD"), OPERATOR, POL)
    assert ei.value.reason == "admin_required"


def test_a_raw_linux_disk_is_still_blank() -> None:
    """lsblk probes whole-disk signatures (FSTYPE), so on Linux 'no partition table' means nothing found."""
    d = replace(raw_disk(), platform="linux", device_path="/dev/sdb")
    assert classify(d, POL, protected_for(), inspector=FakeInspector()).drive_class is DriveClass.BLANK


# ============================================================================ 3. lookalikes behind a serial-hiding dock

def test_two_different_disks_with_hidden_serials_and_the_same_model_and_size_are_refused() -> None:
    a = foreign_disk(disk_id="3", serial=ZEROS, unique_id=None)
    v2 = vol("c0ffee00-0000-0000-0000-0000000000f2", "ntfs", label="Keep", free=2 * 10**12)
    b = usb_disk(disk_id="4", serial=ZEROS, unique_id=None, disk_guid="44444444-0000-0000-0000-000000000000",
                 partitions=(data_part(1, v2, guid="44444444-0000-0000-0000-000000000001"),))
    insp = FakeInspector({x.volumes[0].volume_id: RootContents(readable=True, entries=("f",)) for x in (a, b)})
    assert a.identity.fingerprint() != b.identity.fingerprint()
    classes, _ = classify_all([a, b, SYS], POL, protected_for(), inspector=insp)
    by = {c.disk.disk_id: c for c in classes}
    for k in ("3", "4"):
        assert by[k].drive_class is DriveClass.FOREIGN_DATA and not by[k].can_prepare
        assert by[k].refusal == "lookalike" and "hides both serial numbers" in by[k].message
    with pytest.raises(PrepareRefused) as ei:
        check_guards(by["3"], Confirmation.for_classification(by["3"], "ERASE"), ADMIN, POL)
    assert ei.value.reason == "lookalike"
    # A different size can be told apart in the list: allowed.
    c = replace(b, size_bytes=2 * 10**12)
    classes, _ = classify_all([a, c, SYS], POL, protected_for(), inspector=insp)
    assert all(x.can_prepare for x in classes if x.disk.disk_id in ("3", "4"))


# ============================================================================ 4 + 5. safe removal

class EjectEnv:
    def __init__(self, tmp: Path, disks, inspector=None) -> None:
        self.policy = POL
        self.protected = protected_for()
        self.windows = True
        self.cfg = SimpleNamespace(data_path=tmp / "data")
        self._disks = disks
        self.inspector = inspector or FakeInspector()

    def enumerate(self):
        return list(self._disks)


def _eject_args(*extra: str):
    return cli.build_parser().parse_args(["eject", "--disk", "4", *extra])


@pytest.fixture()
def removals(monkeypatch):
    seen = []

    def fake_safe_remove(d, disks, protected, *, enumerate_fn):
        seen.append(d.disk_id)
        return ej.SafeRemoveResult(True, "SAFE TO REMOVE DRIVE", needs_admin=False)

    monkeypatch.setattr(ej, "safe_remove", fake_safe_remove)
    return seen


def test_safely_removing_an_archive_drive_records_it_like_swap(tmp_path: Path, ledger: Ledger, removals,
                                                              capsys) -> None:
    from sentinel.archive.ledger import ArchiveLedger

    arc, insp = _ready_archive()
    env = EjectEnv(tmp_path, [arc, SYS], insp)
    assert cli.cmd_eject(env, _eject_args()) == cli.EXIT_OK and removals == ["4"]
    assert "recorded as taken out" in capsys.readouterr().out
    ev = _events(ledger, "archive_swap_ready")
    assert ev and ev[0]["drive_id"] == f"drive_uuid:{UID}" and ev[0]["last_copy_id"] == 0
    # ...so Return to service finds the swap it needs (sentinel.archive.worker._return_drive).
    assert ArchiveLedger(ledger, "CAM01").last_swap_since_return(f"drive_uuid:{UID}") == \
        {"ts_utc": ledger.query("SELECT ts_utc FROM events WHERE code='archive_swap_ready'")[0][0], "last_copy_id": 0}
    # ...and the copier's list of drives waiting to be returned, so it does not write to the drive when it
    # comes back before its return (as after Swap).
    from sentinel.archive.worker import AWAITING_STATE, CONTROL_DIR

    doc = json.loads((tmp_path / "data" / CONTROL_DIR / AWAITING_STATE).read_text("utf-8"))
    assert f"drive_uuid:{UID}" in doc["drives"] and f"drive_uuid:{UID}" in doc["since"]


def test_safely_removing_an_archive_drive_while_copying_runs_is_refused(tmp_path: Path, ledger: Ledger, removals,
                                                                       capsys) -> None:
    from sentinel.archive.status import STATUS_FILE

    (tmp_path / "data" / STATUS_FILE).write_text(json.dumps({"state": "COPYING", "heartbeat_epoch": time.time()}),
                                                 encoding="utf-8")
    arc, insp = _ready_archive()
    env = EjectEnv(tmp_path, [arc, SYS], insp)
    assert cli.cmd_eject(env, _eject_args()) == cli.EXIT_REFUSED and removals == []
    assert "Swap archive drive" in capsys.readouterr().out
    assert _events(ledger, "archive_swap_ready") == []


def test_the_drive_a_swap_prepared_can_be_safely_removed_while_copying_runs(tmp_path: Path, ledger: Ledger,
                                                                            removals, capsys) -> None:
    """Field report 2026-11-13: the swap said to remove the drive with Windows (its own eject failed) and Safely
    remove refused it because copying was running. The swap already recorded it, so nothing is recorded twice."""
    from sentinel.archive.status import STATUS_FILE

    doc = {"state": "REMOVE_MANUALLY", "heartbeat_epoch": time.time(),
           "removal": {"drive_id": f"drive_uuid:{UID}", "label": "SENTINEL_4", "safe": False}}
    (tmp_path / "data" / STATUS_FILE).write_text(json.dumps(doc), encoding="utf-8")
    arc, insp = _ready_archive()
    env = EjectEnv(tmp_path, [arc, SYS], insp)
    assert cli.cmd_eject(env, _eject_args()) == cli.EXIT_OK and removals == ["4"]
    assert _events(ledger, "archive_swap_ready") == []
    # Another archive drive than the one prepared is still refused.
    doc["removal"]["drive_id"] = "drive_uuid:someone-else"
    (tmp_path / "data" / STATUS_FILE).write_text(json.dumps(doc), encoding="utf-8")
    assert cli.cmd_eject(env, _eject_args()) == cli.EXIT_REFUSED and removals == ["4"]


def test_safely_removing_another_drive_records_nothing(tmp_path: Path, ledger: Ledger, removals) -> None:
    d = foreign_disk(disk_id="4")
    env = EjectEnv(tmp_path, [d, SYS])
    assert cli.cmd_eject(env, _eject_args()) == cli.EXIT_OK and removals == ["4"]
    assert _events(ledger, "archive_swap_ready") == []


def test_safe_removal_is_bound_to_the_drive_that_was_shown(tmp_path: Path, removals, capsys) -> None:
    shown, _ = _ready_archive()
    now = foreign_disk(disk_id="4")                # another drive got disk number 4 meanwhile
    env = EjectEnv(tmp_path, [now, SYS])
    assert cli.cmd_eject(env, _eject_args("--fingerprint", shown.identity.fingerprint())) == cli.EXIT_REFUSED
    assert removals == [] and "not the drive that was shown" in capsys.readouterr().out
    env = EjectEnv(tmp_path, [now, SYS])
    assert cli.cmd_eject(env, _eject_args("--fingerprint", now.identity.fingerprint())) == cli.EXIT_OK


class FakeCM:
    """1=disk -> 2=removable USB device -> 3=hub; 4=the other disk, in its own enclosure under the hub."""

    def __init__(self) -> None:
        self.parents = {1: 2, 2: 3, 3: None, 4: 5, 5: 3}
        self.ids = {"USBSTOR\\DISK&VEN_WDC\\FAKE": 1, "OTHER": 4}
        self.ejected: list[int] = []

    def locate(self, i):
        return self.ids[i]

    def ancestors(self, n, levels=16):
        out = []
        while self.parents.get(n):
            n = self.parents[n]
            out.append(n)
        return out[:levels]

    def status(self, n):
        return ej.DN_REMOVABLE if n in (2, 5) else 0

    def device_id(self, n):
        return f"node{n}"

    def eject(self, n):
        self.ejected.append(n)
        return 0, 0, ""


def test_a_lookalike_still_connected_does_not_make_a_good_removal_look_failed(monkeypatch) -> None:
    d = blank_disk(serial=ZEROS, unique_id="5000000000000001")
    other = replace(d, disk_id="5", pnp_device_id="OTHER")
    assert other.identity.same_device(d.identity)   # same hardware key: the old check never saw d as gone
    cm = FakeCM()
    monkeypatch.setattr(ej, "_flush_windows", lambda disk, steps: None)
    monkeypatch.setattr(ej, "_CfgMgr", lambda: cm)
    res = ej.safe_remove_windows(d, [d, other], protected_for(), enumerate_fn=lambda: [other], wait_s=1)
    assert res.ok and res.message == "SAFE TO REMOVE DRIVE" and cm.ejected == [2]
    # Still listed (same instance ID): not reported as safe.
    res = ej.safe_remove_windows(d, [d, other], protected_for(), enumerate_fn=lambda: [d, other], wait_s=0.1)
    assert not res.ok and "Do not unplug" in res.message


# ============================================================================ 6. export target, nothing written

def test_a_drive_only_registered_by_an_export_that_wrote_nothing_is_not_evidence(ledger: Ledger) -> None:
    guid = "0f0e0d0c-1111-2222-3333-444455556666"
    d = usb_disk(serial="S1XY9876", unique_id=None, partitions=(data_part(1, vol(guid)),))
    with ledger.tx() as c:
        c.execute("INSERT INTO drives (drive_id, volume_guid, identity, first_seen_utc, last_seen_utc) "
                  "VALUES (?, ?, '{}', 't', 't')", (f"volume_guid:{{{guid}}}", f"{{{guid}}}"))
    assert load_known(ledger).matches(d) == []
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=("T7 software",))})
    assert classify(d, POL, protected_for(), inspector=insp, known=load_known(ledger)).can_prepare
    with ledger.tx() as c:   # once something was exported to it, it is a Sentinel drive
        c.execute("INSERT INTO export_batches (batch_id, purpose, camera_id, drive_id, state, interval_start_utc, "
                  "interval_end_utc, display_timezone, app_version, stamp, planned_utc, updated_utc) VALUES "
                  "('b1', 'review', 'CAM01', ?, 'COPYING', 't', 't', 'UTC', '0', 's', 't', 't')",
                  (f"volume_guid:{{{guid}}}",))
    assert "known_volume" in load_known(ledger).matches(d)


def test_a_drives_row_with_a_drive_uuid_stays_known(ledger: Ledger) -> None:
    with ledger.tx() as c:
        c.execute("INSERT INTO drives (drive_id, drive_uuid, serial, identity, first_seen_utc, last_seen_utc) "
                  "VALUES ('drive_uuid:u-1', 'u-1', 'WD-WCC4E1234ABCD', '{}', 't', 't')")
    assert "wd-wcc4e1234abcd" in load_known(ledger).serials


# ============================================================================ 7. no usable serial

@pytest.mark.parametrize("serial", [None, "AB1"])
def test_a_drive_whose_serial_cannot_be_typed_is_not_offered_for_erasing(serial) -> None:
    d = blank_disk(serial=serial, unique_id=r"USBSTOR\DISK&VEN_CHEAP&PROD_BOX\7&2A3B4C5D&0")
    c = classify(d, POL, protected_for(), inspector=FakeInspector())
    assert not c.can_prepare and c.refusal == "no_confirmation" and "serial number" in c.message
    with pytest.raises(PrepareRefused) as ei:
        check_guards(c, Confirmation.for_classification(c, "ERASE"), ADMIN, POL)
    assert ei.value.reason == "no_confirmation" and "Nothing was erased" in str(ei.value)
    # A dock that hides the serial is still confirmed with ERASE.
    assert classify(blank_disk(serial=ZEROS), POL, protected_for(), inspector=FakeInspector()).can_prepare


# ============================================================================ 8. helper result missing

def _prep_ctx(led, d, runner, tmp_path):
    return PrepareContext(policy=POL, protected=protected_for(), enumerate=Sequence([d, SYS]), runner=runner,
                          audit=led, platform="windows", inspector=FakeInspector(), settle_s=0.0,
                          work_dir=tmp_path / "work")


def _conf(d):
    return Confirmation.for_classification(classify(d, POL, protected_for(), inspector=FakeInspector()), "ABCD")


@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_a_missing_helper_result_does_not_claim_nothing_was_erased(ledger: Ledger, tmp_path: Path,
                                                                    exit_code: int) -> None:
    d = blank_disk()
    with pytest.raises(PrepareFailed) as ei:
        prepare_drive(_prep_ctx(ledger, d, FakeRunner(HelperResult(exit_code, None)), tmp_path), d.disk_id,
                      _conf(d), OPERATOR)
    assert ei.value.reason == "outcome_unknown" and "Nothing was erased" not in str(ei.value)
    assert _events(ledger, "drive_prepare_failed")[0]["erase_started"] is True


def test_a_declined_prompt_without_a_result_is_still_nothing_erased(ledger: Ledger, tmp_path: Path) -> None:
    d = blank_disk()
    with pytest.raises(PrepareRefused) as ei:
        prepare_drive(_prep_ctx(ledger, d, FakeRunner(HelperResult(EXIT_ELEVATION_CANCELLED, None)), tmp_path),
                      d.disk_id, _conf(d), OPERATOR)
    assert ei.value.reason == "elevation_cancelled"
    # But a helper that reported erasing is never "approval was not given".
    with pytest.raises(PrepareFailed) as ei2:
        prepare_drive(_prep_ctx(ledger, d, FakeRunner(HelperResult(EXIT_ELEVATION_CANCELLED,
                                                                   {"ok": False, "step": "clear"})), tmp_path),
                      d.disk_id, _conf(d), OPERATOR)
    assert ei2.value.reason == "prepare_incomplete"


def test_linux_script_always_reports_how_far_it_got() -> None:
    d = replace(raw_disk(), platform="linux", device_path="/dev/sdb")
    s = build_linux_script(d, filesystem="exfat", label="SSD_2", protected_devices=())
    assert "trap '" in s and "error=aborted_$rc" in s
    assert s.count("udevadm settle || fail 2 udevadm_settle_failed") == 2
    # pkexec's 127 (dismissed) and a command missing inside the script (127 too) are told apart.
    assert linux_helper_result(127, "", "").exit_code == EXIT_ELEVATION_CANCELLED
    after = linux_helper_result(127, "RESULT ok=0 step=partition code=2 error=aborted_127\n", "")
    assert after.exit_code == 127 and after.result["step"] == "partition"


# ============================================================================ 9. records written first, lock retried

def _drive(tmp: Path, name: str, label: str, led: Ledger) -> tuple[Path, str]:
    import uuid

    root = tmp / name
    root.mkdir()
    uid = str(uuid.uuid4())
    write_marker(root, {**ARCHIVE_MARKER, "drive_uuid": uid, "label": label})
    led.add_event("audit", "drives", "drive_prepared", f"Drive prepared as {label}",
                  {"label": label, "drive_uuid": uid})
    return root, uid


def _marker(root: Path) -> dict:
    return json.loads((root / MARKER_NAME).read_text("utf-8"))


def test_a_locked_ledger_never_leaves_a_renamed_drive_without_a_record(tmp_path: Path, ledger: Ledger,
                                                                       no_sleep) -> None:
    for i in range(1, 7):
        _drive(tmp_path, f"d{i}", f"SSD_{i}", ledger)
    root = tmp_path / "d2"
    before = _marker(root)
    labels: list[str] = []
    locked = Locked(ledger, rn.RENAMED)
    with pytest.raises(rn.RenameRefused, match="keeps its name SSD_2"):
        rn.rename_drive(root, "SSD_7", "admin", locked, DrivePolicy(), set_label=lambda _r, n: labels.append(n))
    assert locked.attempts == 8                                    # retried while locked
    assert labels == ["SSD_7", "SSD_2"] and _marker(root) == before  # put back as it was
    assert _events(ledger, "drive_rename_authorized")[0]["new_label"] == "SSD_7"
    # The number stays reserved: the next new drive does not get SSD_7.
    assert next_label(DrivePolicy(), ledger, []) == "SSD_8"
    # Once the ledger is free the rename works.
    rn.rename_drive(root, "SSD_7", "admin", ledger, DrivePolicy(), set_label=lambda *_a: None)
    assert rn.current_names(ledger)[before["drive_uuid"]] == "SSD_7"


def test_when_the_drive_cannot_be_put_back_the_same_rename_records_it(tmp_path: Path, ledger: Ledger,
                                                                     no_sleep) -> None:
    root, uid = _drive(tmp_path, "t7", "SSD_3", ledger)

    def set_label(_r: str, n: str) -> None:
        if n == "SSD_3":
            raise OSError(5, "access denied")
    with pytest.raises(rn.RenameNotRecorded):
        rn.rename_drive(root, "SSD_9", "admin", Locked(ledger, rn.RENAMED), DrivePolicy(), set_label=set_label)
    assert _marker(root)["label"] == "SSD_9"
    # Another drive cannot take the name meanwhile...
    other, _ = _drive(tmp_path, "t8", "SSD_4", ledger)
    with pytest.raises(rn.RenameRefused, match="records is already called SSD_9"):
        rn.rename_drive(other, "SSD_9", "admin", ledger, DrivePolicy(), set_label=lambda *_a: None)
    # ...and running the same rename again writes the missing record.
    detail = rn.rename_drive(root, "SSD_9", "admin", ledger, DrivePolicy(), set_label=lambda *_a: pytest.fail("x"))
    assert detail["recorded_late"] and rn.current_names(ledger)[uid] == "SSD_9"
    with pytest.raises(rn.RenameRefused, match="already called SSD_9"):
        rn.rename_drive(root, "SSD_9", "admin", ledger, DrivePolicy(), set_label=lambda *_a: None)


def test_a_rename_that_cannot_be_recorded_first_changes_nothing(tmp_path: Path, ledger: Ledger, no_sleep) -> None:
    root, _ = _drive(tmp_path, "t7", "SSD_3", ledger)
    before = _marker(root)
    with pytest.raises(rn.RenameRefused, match="Nothing was changed"):
        rn.rename_drive(root, "SSD_9", "admin", Locked(ledger, rn.RENAME_AUTHORIZED), DrivePolicy(),
                        set_label=lambda *_a: pytest.fail("label changed"))
    assert _marker(root) == before


def _success_world(d, root: Path):
    after = prepared_disk_like(d, "SSD_1")
    enum = Sequence([d, SYS], [d, SYS])
    runner = FakeRunner(HelperResult(0, {"ok": True, "step": "done"}),
                        on_run=lambda plan: enum.snapshots.append([after, SYS]) or
                        setattr(enum, "calls", len(enum.snapshots) - 1))
    root.mkdir(parents=True, exist_ok=True)
    return enum, runner


def _ctx(audit, enum, runner, root: Path) -> PrepareContext:
    from sentinel.drives.marker import inspect_root

    def insp(v):
        return inspect_root(root) if v.label and v.label.startswith("SSD_") else RootContents(readable=True)
    return PrepareContext(policy=POL, protected=protected_for(), enumerate=enum, runner=runner, audit=audit,
                          platform="windows", inspector=insp, root_for_volume=lambda v: str(root), settle_s=0.0,
                          work_dir=root.parent / "work")


def test_a_set_up_drive_whose_record_fails_keeps_its_number(tmp_path: Path, ledger: Ledger, no_sleep) -> None:
    d = blank_disk()
    enum, runner = _success_world(d, tmp_path / "vol")
    with pytest.raises(PrepareFailed) as ei:
        prepare_drive(_ctx(Locked(ledger, "drive_prepared"), enum, runner, tmp_path / "vol"), d.disk_id,
                      _conf(d), OPERATOR)
    assert ei.value.reason == "not_recorded" and "SSD_1" in str(ei.value)
    assert next_label(POL, ledger, []) == "SSD_2"


def test_a_set_up_that_cannot_be_recorded_first_erases_nothing(tmp_path: Path, ledger: Ledger, no_sleep) -> None:
    d = blank_disk()
    enum, runner = _success_world(d, tmp_path / "vol")
    with pytest.raises(PrepareRefused) as ei:
        prepare_drive(_ctx(Locked(ledger, "drive_prepare_authorized"), enum, runner, tmp_path / "vol"),
                      d.disk_id, _conf(d), OPERATOR)
    assert ei.value.reason == "record_failed" and "Nothing was erased" in str(ei.value) and runner.plans == []
    assert next_label(POL, ledger, []) == "SSD_1"


def test_a_failed_set_up_frees_its_number_again(tmp_path: Path, ledger: Ledger) -> None:
    d = blank_disk()
    with pytest.raises(PrepareFailed):
        prepare_drive(_prep_ctx(ledger, d, FakeRunner(HelperResult(2, {"ok": False, "step": "format"})), tmp_path),
                      d.disk_id, _conf(d), OPERATOR)
    assert next_label(POL, ledger, []) == "SSD_1"


# ============================================================================ 10. serial from Win32_DiskDrive

def test_the_listing_and_the_helper_take_the_serial_from_the_same_places() -> None:
    data = load_fixture("windows_dev_host.json")
    disk = next(x for x in data["disks"] if x.get("SerialNumber") and str(x["SerialNumber"]).strip())
    disk["SerialNumber"] = None
    d = next(x for x in parse_windows_snapshot(data) if x.disk_id == str(disk["Number"]))
    wmi = next(x for x in data["disk_drives"] if x["Index"] == disk["Number"])
    assert d.serial and d.serial == "".join(str(wmi["SerialNumber"]).split()).rstrip(".")
    assert "function Disk-Serial" in WINDOWS_TEMPLATE and "$serial = Disk-Serial $d" in WINDOWS_TEMPLATE
    assert "Win32_DiskDrive -Filter ('Index={0}' -f [int]$d.Number)" in WINDOWS_TEMPLATE


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows PowerShell")
def test_disk_serial_falls_back_to_win32_diskdrive_in_real_powershell() -> None:
    import re

    from sentinel.drives.powershell import run_powershell

    fns = "".join(re.search(rf"function {n}\(.*?\n(?=function )", WINDOWS_TEMPLATE, re.S).group(0)
                  for n in ("N", "Norm-Serial", "Disk-Serial"))
    # A stand-in for the cmdlet (functions win over cmdlets): no disk is queried.
    stub = ("function Get-CimInstance { param($ClassName, $Filter) "
            "if ($Filter -eq 'Index=7') { [pscustomobject]@{ SerialNumber = '  WMI 1234.  ' } } }\n")
    cases = ("@((Disk-Serial ([pscustomobject]@{ SerialNumber = ''; Number = 7 })), "
             "(Disk-Serial ([pscustomobject]@{ SerialNumber = ' Z505GDE8 '; Number = 7 })), "
             "(Disk-Serial ([pscustomobject]@{ SerialNumber = $null; Number = 8 }))) | ConvertTo-Json -Compress")
    assert json.loads(run_powershell(fns + stub + cases)) == ["WMI1234", "Z505GDE8", ""]
    d = usb_disk(serial="WMI1234")
    assert windows_expected(d, filesystem="ntfs", label="SSD_1", protected_volume_ids=(),
                            result_path="r")["serial"] == "WMI1234"


# ============================================================================ 11. rename bound to the selected drive

def test_rename_refuses_a_drive_other_than_the_one_selected(tmp_path: Path, ledger: Ledger) -> None:
    root, uid = _drive(tmp_path, "e", "SSD_3", ledger)
    before = _marker(root)
    with pytest.raises(rn.RenameRefused, match="not the drive that was selected"):
        rn.rename_drive(root, "SSD_9", "admin", ledger, DrivePolicy(),
                        expect_uuid="11111111-2222-4333-8444-555555555555",
                        set_label=lambda *_a: pytest.fail("label changed"))
    assert _marker(root) == before and not _events(ledger, "drive_rename_authorized")
    rn.rename_drive(root, "SSD_9", "admin", ledger, DrivePolicy(), expect_uuid=uid.upper(), set_label=lambda *_a: None)
    assert _marker(root)["label"] == "SSD_9"


def test_rename_command_passes_the_expected_drive(tmp_path: Path, monkeypatch, capsys) -> None:
    import sentinel.drives.prepare as prep

    seen = {}

    def fake_rename(root, name, who, audit, policy, *, other_labels=(), expect_uuid=None):
        seen["expect_uuid"] = expect_uuid
        raise rn.RenameRefused("refused for the test. Nothing was changed.")
    monkeypatch.setattr(prep, "os_administrator_check", lambda: (True, "test"))
    monkeypatch.setattr(rn, "rename_drive", fake_rename)
    env = SimpleNamespace(cfg=SimpleNamespace(data_path=tmp_path), windows=True, policy=DrivePolicy(),
                          enumerate=lambda: [])
    args = cli.build_parser().parse_args(["rename", "--drive", "E:", "--name", "SSD_1", "--expect-uuid", UID])
    assert cli.cmd_rename(env, args) == cli.EXIT_REFUSED and seen["expect_uuid"] == UID
