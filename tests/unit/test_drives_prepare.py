"""'Prepare This Drive' guards and orchestration with a fake elevated runner.

No real disk is touched: enumeration is a fake sequence of snapshots, the elevated helper is a
FakeRunner, and the "prepared volume" root is a pytest temporary directory.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from drive_fakes import (
    TB,
    FakeInspector,
    FakeRunner,
    Sequence,
    blank_disk,
    data_part,
    exfat_disk,
    foreign_disk,
    msr_part,
    prepared_disk_like,
    protected_for,
    system_disk,
    usb_disk,
    vol,
)

from sentinel.db import Ledger
from sentinel.drives.classify import classify
from sentinel.drives.errors import IdentityChanged, PrepareFailed, PrepareRefused
from sentinel.drives.marker import MARKER_NAME, RootContents, inspect_root
from sentinel.drives.model import DriveClass
from sentinel.drives.policy import DrivePolicy, load_policy
from sentinel.drives.prepare import (
    Authorization,
    Confirmation,
    HelperResult,
    PrepareContext,
    check_guards,
    next_label,
    prepare_drive,
)

OK = HelperResult(0, {"ok": True, "step": "done"})
OPERATOR = Authorization("operator7", administrator=False, method="test")
ADMIN = Authorization("admin1", administrator=True, method="test")
SYS = system_disk()


@pytest.fixture()
def ledger(tmp_path: Path):
    led = Ledger(tmp_path / "data" / "sentinel.db")
    yield led
    led.close()


def _ctx(ledger: Ledger, enum: Sequence, runner: FakeRunner, root: Path, *, policy: DrivePolicy | None = None,
         inspector=None) -> PrepareContext:
    def insp(v):
        # The 'prepared' volume is backed by a real temporary directory; others use the fake.
        if v.label and v.label.startswith("SSD_"):
            return inspect_root(root)
        return (inspector or FakeInspector())(v)

    return PrepareContext(policy=policy or DrivePolicy(erase_enabled=True), protected=protected_for(), enumerate=enum, runner=runner,
                          audit=ledger, platform="windows", inspector=insp, root_for_volume=lambda v: str(root),
                          settle_s=0.0, work_dir=root.parent / "work")


def _conf(d, typed=None, policy=None, inspector=None) -> Confirmation:
    c = classify(d, policy or DrivePolicy(erase_enabled=True), protected_for(), inspector=inspector or FakeInspector())
    return Confirmation.for_classification(c, typed)


def _events(ledger: Ledger, code: str) -> list[dict]:
    return [json.loads(r["detail"]) for r in ledger.query("SELECT detail FROM events WHERE code=?", (code,))]


def _success_world(d, root: Path):
    after = prepared_disk_like(d, "SSD_1")
    enum = Sequence([d, SYS], [d, SYS])
    runner = FakeRunner(OK, on_run=lambda plan: enum.snapshots.append([after, SYS]) or
                        setattr(enum, "calls", len(enum.snapshots) - 1))
    root.mkdir(parents=True, exist_ok=True)
    return enum, runner


# ---------------------------------------------------------------------------- success path

def test_blank_drive_prepared_end_to_end(ledger: Ledger, tmp_path: Path) -> None:
    d = blank_disk()
    root = tmp_path / "vol"
    enum, runner = _success_world(d, root)
    res = prepare_drive(_ctx(ledger, enum, runner, root), d.disk_id, _conf(d), OPERATOR)
    assert res.label == "SSD_1" and res.filesystem == "ntfs" and res.previous_class == "BLANK"
    assert len(runner.plans) == 1 and runner.plans[0].identity == d.identity
    marker = json.loads((root / MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["drive_uuid"] == res.drive_uuid and marker["prepared_by"] == "operator7"
    assert marker["label"] == "SSD_1" and marker["write_test"]["bytes"] == 1024 * 1024
    assert not (root / "SENTINEL_WRITE_TEST.bin").exists()
    done = _events(ledger, "drive_prepared")
    assert len(done) == 1 and done[0]["who"] == "operator7" and done[0]["disk"]["serial"] == d.serial
    assert len(_events(ledger, "drive_prepare_authorized")) == 1
    assert ledger.query("SELECT severity, category FROM events WHERE code='drive_prepared'")[0][:] == ("audit", "drives")
    assert next_label(DrivePolicy(erase_enabled=True), ledger, []) == "SSD_2"


def test_wrong_format_needs_single_confirmation_only(ledger: Ledger, tmp_path: Path) -> None:
    d = exfat_disk()
    root = tmp_path / "vol"
    enum, runner = _success_world(d, root)
    res = prepare_drive(_ctx(ledger, enum, runner, root), d.disk_id, _conf(d), OPERATOR)
    assert res.previous_class == "WRONG_FORMAT"


def test_foreign_with_admin_and_serial(ledger: Ledger, tmp_path: Path) -> None:
    d = foreign_disk()
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=("Photos",))})
    root = tmp_path / "vol"
    enum, runner = _success_world(d, root)
    res = prepare_drive(_ctx(ledger, enum, runner, root, inspector=insp), d.disk_id,
                        _conf(d, "abcd", inspector=insp), ADMIN)
    assert res.previous_class == "FOREIGN_DATA"
    ev = _events(ledger, "drive_prepared")[0]
    assert ev["administrator"] is True and ev["class"] == "FOREIGN_DATA" and ev["foreign_policy"] == "admin_only"


# ---------------------------------------------------------------------------- guards (runner never called)

def _refused(ledger, tmp_path, d, conf, auth, *, enum=None, policy=None, inspector=None, exc=PrepareRefused):
    runner = FakeRunner(OK)
    ctx = _ctx(ledger, enum or Sequence([d, SYS]), runner, tmp_path / "vol", policy=policy, inspector=inspector)
    with pytest.raises(exc) as ei:
        prepare_drive(ctx, d.disk_id, conf, auth)
    assert runner.plans == [], "the elevated helper must not run"
    assert _events(ledger, "drive_prepared") == []
    return ei.value


def test_never_runs_without_confirmation(ledger: Ledger, tmp_path: Path) -> None:
    e = _refused(ledger, tmp_path, blank_disk(), None, OPERATOR)
    assert e.reason == "not_confirmed"


def test_never_runs_without_a_person(ledger: Ledger, tmp_path: Path) -> None:
    d = blank_disk()
    assert _refused(ledger, tmp_path, d, _conf(d), Authorization(" ", False, "t")).reason == "no_operator"


def test_system_disk_refused(ledger: Ledger, tmp_path: Path) -> None:
    conf = Confirmation(SYS.disk_id, SYS.identity.fingerprint(), DriveClass.BLANK)
    e = _refused(ledger, tmp_path, SYS, conf, ADMIN, enum=Sequence([SYS]))
    assert e.reason == "system_disk"


def test_spool_disk_refused_by_identity(ledger: Ledger, tmp_path: Path) -> None:
    d = exfat_disk()
    runner = FakeRunner(OK)
    ctx = _ctx(ledger, Sequence([d, SYS]), runner, tmp_path / "vol")
    ctx.protected = protected_for(spool_vol=d.volumes[0].volume_id)
    conf = Confirmation(d.disk_id, d.identity.fingerprint(), DriveClass.WRONG_FORMAT)
    with pytest.raises(PrepareRefused) as ei:
        prepare_drive(ctx, d.disk_id, conf, ADMIN)
    assert ei.value.reason == "recording_storage"
    assert runner.plans == []


def test_ready_drive_refused(ledger: Ledger, tmp_path: Path) -> None:
    d = prepared_disk_like(blank_disk(), "SSD_4")
    marker = {"format": "sentinel-drive", "version": 1, "drive_uuid": "0b0e5f0a-1c2d-4e3f-8a9b-0c1d2e3f4a5b",
              "label": "SSD_4", "prepared_utc": "x", "prepared_by": "y"}
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=(MARKER_NAME,), marker=marker)})
    runner = FakeRunner(OK)
    ctx = PrepareContext(policy=DrivePolicy(erase_enabled=True), protected=protected_for(), enumerate=Sequence([d, SYS]), runner=runner,
                         audit=ledger, platform="windows", inspector=insp, settle_s=0)
    with pytest.raises(PrepareRefused) as ei:
        prepare_drive(ctx, d.disk_id, Confirmation(d.disk_id, d.identity.fingerprint(), DriveClass.READY), ADMIN)
    assert ei.value.reason == "already_prepared" and runner.plans == []


def test_foreign_requires_admin_under_default_policy(ledger: Ledger, tmp_path: Path) -> None:
    d = foreign_disk()
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=("x",))})
    e = _refused(ledger, tmp_path, d, _conf(d, "ABCD", inspector=insp), OPERATOR, inspector=insp)
    assert e.reason == "admin_required"


@pytest.mark.parametrize("typed", [None, "", "ABCE", "WCC4", "abc"])
def test_foreign_serial_mismatch_refused(ledger: Ledger, tmp_path: Path, typed) -> None:
    d = foreign_disk()
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=("x",))})
    e = _refused(ledger, tmp_path, d, _conf(d, typed, inspector=insp), ADMIN, inspector=insp)
    assert e.reason == "serial_mismatch"


def test_policy_never_refuses_even_administrators(ledger: Ledger, tmp_path: Path) -> None:
    d = foreign_disk()
    pol = load_policy({"erase_enabled": True, "foreign_data_authorization": "never"})
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=("x",))})
    e = _refused(ledger, tmp_path, d, _conf(d, "ABCD", policy=pol, inspector=insp), ADMIN, policy=pol, inspector=insp)
    assert e.reason == "policy_never"


def test_operator_with_serial_policy_allows_operator(ledger: Ledger, tmp_path: Path) -> None:
    d = foreign_disk()
    pol = load_policy({"erase_enabled": True, "foreign_data_authorization": "operator_with_serial"})
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=("x",))})
    root = tmp_path / "vol"
    enum, runner = _success_world(d, root)
    res = prepare_drive(_ctx(ledger, enum, runner, root, policy=pol, inspector=insp), d.disk_id,
                        _conf(d, "abcd", policy=pol, inspector=insp), OPERATOR)
    assert res.previous_class == "FOREIGN_DATA"
    with pytest.raises(PrepareRefused):  # but still needs the serial
        check_guards(classify(d, pol, protected_for(), inspector=insp), _conf(d, None, policy=pol, inspector=insp),
                     OPERATOR, pol)


def test_drive_swapped_after_confirmation_refused(ledger: Ledger, tmp_path: Path) -> None:
    confirmed = blank_disk()
    swapped = blank_disk(serial="WD-OTHER9999", unique_id="5000CCA0FFFFFFFF")  # same disk number
    e = _refused(ledger, tmp_path, confirmed, _conf(confirmed), OPERATOR, enum=Sequence([swapped, SYS]),
                 exc=IdentityChanged)
    assert e.reason == "identity_changed" and e.code == "M-006"


def test_identity_changed_between_guard_and_execute_refused(ledger: Ledger, tmp_path: Path) -> None:
    """TOCTOU: identical at the first check, swapped at the re-check just before the helper."""
    d = blank_disk()
    swapped = blank_disk(serial="WD-OTHER9999")
    e = _refused(ledger, tmp_path, d, _conf(d), OPERATOR, enum=Sequence([d, SYS], [swapped, SYS]), exc=IdentityChanged)
    assert e.reason == "identity_changed"
    refused = _events(ledger, "drive_prepare_refused")
    assert len(refused) == 1 and refused[0]["reason"] == "identity_changed"


def test_files_added_after_confirmation_refused(ledger: Ledger, tmp_path: Path) -> None:
    d = exfat_disk()
    conf = _conf(d)  # WRONG_FORMAT when shown
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=("new.doc",))})
    e = _refused(ledger, tmp_path, d, conf, ADMIN, inspector=insp, exc=IdentityChanged)
    assert e.reason == "class_changed"


def test_drive_removed_refused(ledger: Ledger, tmp_path: Path) -> None:
    d = blank_disk()
    e = _refused(ledger, tmp_path, d, _conf(d), OPERATOR, enum=Sequence([SYS]), exc=IdentityChanged)
    assert e.reason == "disk_gone"


def test_confirmation_for_other_disk_number_refused(ledger: Ledger, tmp_path: Path) -> None:
    d = blank_disk()
    conf = replace(_conf(d), disk_id="8")
    assert _refused(ledger, tmp_path, d, conf, OPERATOR, exc=IdentityChanged).reason == "identity_changed"


# ---------------------------------------------------------------------------- helper outcomes

@pytest.mark.parametrize("code,step,exc,reason", [
    (3, "verify", IdentityChanged, "identity_changed"),
    (4, "verify", PrepareRefused, "protected"),
    (1223, "start", PrepareRefused, "elevation_cancelled"),
    (2, "verify", PrepareFailed, "helper_failed"),
    (2, "format", PrepareFailed, "prepare_incomplete"),
])
def test_helper_failures(ledger: Ledger, tmp_path: Path, code: int, step: str, exc, reason: str) -> None:
    d = blank_disk()
    runner = FakeRunner(HelperResult(code, {"ok": False, "step": step, "error": "x"}))
    ctx = _ctx(ledger, Sequence([d, SYS]), runner, tmp_path / "vol")
    with pytest.raises(exc) as ei:
        prepare_drive(ctx, d.disk_id, _conf(d), OPERATOR)
    assert ei.value.reason == reason and len(runner.plans) == 1
    failed = _events(ledger, "drive_prepare_failed")
    assert len(failed) == 1 and failed[0]["erase_started"] is (step not in ("start", "verify"))
    assert _events(ledger, "drive_prepared") == []


def test_wrong_layout_after_helper_is_not_marked_ready(ledger: Ledger, tmp_path: Path) -> None:
    d = blank_disk()
    root = tmp_path / "vol"
    root.mkdir()
    after = prepared_disk_like(d, "SOMETHING_ELSE")
    enum = Sequence([d, SYS], [d, SYS], [after, SYS])
    with pytest.raises(PrepareFailed) as ei:
        prepare_drive(_ctx(ledger, enum, FakeRunner(OK), root), d.disk_id, _conf(d), OPERATOR)
    assert ei.value.reason == "layout_mismatch"
    assert not (root / MARKER_NAME).exists()


def test_linux_flow_mounts_then_writes_marker(ledger: Ledger, tmp_path: Path) -> None:
    """Linux orchestration with fakes (the pkexec runner and udisksctl themselves are NOT EXECUTED)."""
    from sentinel.drives.protected import ROLE_OS, ROLE_SPOOL, ProtectedItem, ProtectedStorage

    d = replace(blank_disk(), platform="linux", disk_id="sdb", device_path="/dev/sdb", os_devices=("8:16",))
    sysd = replace(system_disk(), platform="linux", disk_id="nvme0n1", os_devices=("259:0", "252:0"))
    after = prepared_disk_like(d, "SSD_1")
    root = tmp_path / "media"
    root.mkdir()
    mounted = replace(after, partitions=tuple(
        replace(p, volume=replace(p.volume, mount_paths=(str(root),))) if p.volume else p for p in after.partitions))
    enum = Sequence([d, sysd], [d, sysd])
    mounts = []

    def on_run(plan):
        assert plan.platform == "linux" and "wipefs --all" in plan.script
        enum.snapshots.append([after, sysd])
        enum.calls = len(enum.snapshots) - 1

    def mount(v):
        mounts.append(v.volume_id)
        enum.snapshots.append([mounted, sysd])
        enum.calls = len(enum.snapshots) - 1

    prot = ProtectedStorage((ProtectedItem(ROLE_OS, "/", os_device="252:0"),
                             ProtectedItem(ROLE_SPOOL, "/var/lib/sentinel", os_device="259:0")))
    ctx = PrepareContext(policy=DrivePolicy(erase_enabled=True), protected=prot, enumerate=enum, runner=FakeRunner(OK, on_run=on_run),
                         audit=ledger, platform="linux", inspector=lambda v: inspect_root(root) if v.mount_paths
                         and v.mount_paths[0] == str(root) else RootContents(readable=True), mount_volume=mount,
                         settle_s=0.0)
    conf = Confirmation(d.disk_id, d.identity.fingerprint(), DriveClass.BLANK)
    res = prepare_drive(ctx, d.disk_id, conf, OPERATOR)
    assert mounts and (root / MARKER_NAME).exists() and res.label == "SSD_1"


def test_existing_marker_is_never_overwritten(tmp_path: Path) -> None:
    from sentinel.drives.marker import write_marker

    m = {"format": "sentinel-drive", "version": 1, "drive_uuid": "0b0e5f0a-1c2d-4e3f-8a9b-0c1d2e3f4a5b",
         "label": "SSD_1", "prepared_utc": "x", "prepared_by": "y"}
    write_marker(tmp_path, m)
    with pytest.raises(FileExistsError):
        write_marker(tmp_path, {**m, "label": "SSD_2"})


def test_write_read_back_test_bypasses_cache_on_this_host(tmp_path: Path) -> None:
    import sys

    from sentinel.drives.marker import write_read_back_test

    res = write_read_back_test(tmp_path)
    assert res["bytes"] == 1024 * 1024 and len(res["sha256"]) == 64
    if sys.platform == "win32":
        assert res["uncached"] is True, res.get("uncached_error")
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------- two-bay dock (field report 2026-11-11)

DOCK = "000000000000000000"   # Sabrent EC-HD2B: the same placeholder serial for both bays


ARCHIVE_MARKER = {"format": "sentinel-drive", "version": 1, "drive_uuid": "41414141-aaaa-4bbb-8ccc-000000000001",
                  "label": "SSD_1", "prepared_utc": "2026-11-11T00:00:00Z", "prepared_by": "tech", "role": "archive"}


def _dock_world(ledger: Ledger, tmp_path: Path, helper_guid: str | None):
    """Disk 4: archive drive SSD_1. Disk 3: same model and size in the other bay, left with an empty
    volume labelled SSD_3 by a setup that did not finish (as on the laptop)."""
    same = {"serial": DOCK, "unique_id": None, "model": "Dual SATA Bridge", "size": 2 * TB}
    old_vol = vol("1a1a1a1a-0000-0000-0000-000000000001", label="SSD_1", letter="E")
    archive = usb_disk(disk_id="4", disk_guid="41414141-0000-0000-0000-000000000000",
                       partitions=(msr_part(), data_part(2, old_vol, guid="41414141-0000-0000-0000-000000000002")),
                       **same)
    left_vol = vol("3c3c3c3c-0000-0000-0000-000000000001", label="SSD_3", letter="F")
    target = usb_disk(disk_id="3", disk_guid="31313131-0000-0000-0000-000000000000",
                      partitions=(msr_part(), data_part(2, left_vol, guid="31313131-0000-0000-0000-000000000002")),
                      **same)
    new_vol = vol("5e5e5e5e-0000-0000-0000-000000000001", label="SSD_2", letter="F")
    after = replace(target, disk_guid="32323232-0000-0000-0000-000000000000",
                    partitions=(msr_part(), data_part(2, new_vol, guid="32323232-0000-0000-0000-000000000002")))
    roots = {old_vol.volume_id: tmp_path / "old", left_vol.volume_id: tmp_path / "left",
             new_vol.volume_id: tmp_path / "new"}
    for r in roots.values():
        r.mkdir()
    # The archive drive carries its marker (READY), so the other bay is not refused as its lookalike.
    (roots[old_vol.volume_id] / MARKER_NAME).write_text(json.dumps(ARCHIVE_MARKER), encoding="utf-8")
    insp = lambda v: inspect_root(roots[v.volume_id]) if v.volume_id in roots else RootContents(readable=True)  # noqa: E731
    enum = Sequence([archive, target, SYS], [archive, target, SYS])
    res = {"ok": True, "step": "done"}
    if helper_guid:
        res["partition_guid"] = helper_guid
    runner = FakeRunner(HelperResult(0, res), on_run=lambda plan: enum.snapshots.append([after, archive, SYS]) or
                        setattr(enum, "calls", len(enum.snapshots) - 1))
    ctx = PrepareContext(policy=DrivePolicy(erase_enabled=True), protected=protected_for(), enumerate=enum,
                         runner=runner, audit=ledger, platform="windows", inspector=insp,
                         root_for_volume=lambda v: str(roots[v.volume_id]), settle_s=0.0, work_dir=tmp_path / "work")
    conf = Confirmation.for_classification(classify(target, DrivePolicy(erase_enabled=True), protected_for(),
                                                    inspector=insp))
    return ctx, conf, target, archive, roots[new_vol.volume_id]



def test_second_bay_of_a_dock_is_found_again_by_its_new_partition(ledger: Ledger, tmp_path: Path) -> None:
    ctx, conf, target, archive, new_root = _dock_world(ledger, tmp_path, "{32323232-0000-0000-0000-000000000002}")
    assert target.identity.same_device(archive.identity)   # the dock makes both bays look the same
    assert conf.drive_class is DriveClass.BLANK            # empty volume left by the failed setup
    res = prepare_drive(ctx, "3", conf, OPERATOR)
    # SSD_3 was only on the disk being erased (never recorded as prepared): the next number is 2.
    assert res.label == "SSD_2" and res.disk_id == "3"
    assert json.loads((new_root / MARKER_NAME).read_text(encoding="utf-8"))["label"] == "SSD_2"
    assert json.loads((tmp_path / "old" / MARKER_NAME).read_text(encoding="utf-8")) == ARCHIVE_MARKER
    assert len(_events(ledger, "drive_prepared")) == 1


def test_without_the_new_partition_two_dock_bays_stay_ambiguous(ledger: Ledger, tmp_path: Path) -> None:
    ctx, conf, _target, _archive, new_root = _dock_world(ledger, tmp_path, None)
    with pytest.raises(PrepareFailed) as e:
        prepare_drive(ctx, "3", conf, OPERATOR)
    assert e.value.reason == "ambiguous_identity"
    assert not (new_root / MARKER_NAME).exists()
