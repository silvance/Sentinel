"""Drive enumeration parsing and classification (docs/04 §4). No real disk is touched."""

from __future__ import annotations

from dataclasses import replace

import pytest
from drive_fakes import (
    SYSTEM_VOL,
    FakeInspector,
    blank_disk,
    data_part,
    exfat_disk,
    foreign_disk,
    load_fixture,
    msr_part,
    protected_for,
    usb_disk,
    vol,
)

from sentinel.drives.classify import classify, classify_all
from sentinel.drives.linux_enum import parse_lsblk
from sentinel.drives.marker import MARKER_FORMAT, RootContents
from sentinel.drives.model import (
    GPT_EFI,
    KIND_EFI,
    DriveClass,
    PartitionInfo,
    norm_serial,
    serial_tail,
)
from sentinel.drives.policy import DrivePolicy, DrivePolicyError, load_policy
from sentinel.drives.protected import ROLE_OS, ROLE_SPOOL, ProtectedItem, ProtectedStorage
from sentinel.drives.windows_enum import parse_windows_snapshot

POL = DrivePolicy()
ALL_BUSES = DrivePolicy(allowed_bus_types=("USB", "SATA", "NVME", "FILE BACKED VIRTUAL"), min_size_gb=1)
MARKER = {"format": MARKER_FORMAT, "version": 1, "drive_uuid": "0b0e5f0a-1c2d-4e3f-8a9b-0c1d2e3f4a5b",
          "label": "SENTINEL_3", "prepared_utc": "2026-11-09T00:00:00.000000Z", "prepared_by": "admin1"}


# ---------------------------------------------------------------------------- real host fixture

@pytest.fixture(scope="module")
def host_disks():
    return parse_windows_snapshot(load_fixture("windows_dev_host.json"))


def test_real_fixture_parses_every_disk(host_disks) -> None:
    assert [d.disk_id for d in host_disks] == ["0", "1", "2", "3", "4", "5", "7"]
    by = {d.disk_id: d for d in host_disks}
    assert by["1"].serial == "Z505GDE8"                    # Get-Disk pads it with 12 spaces
    assert by["4"].serial == "6479_A777_3000_0009"          # NVMe trailing '.' removed
    assert by["2"].is_system and by["2"].is_boot
    kinds = [p.kind for p in by["2"].partitions]
    assert kinds == ["efi", "msr", "data", "recovery"]
    c_vol = by["2"].partitions[2].volume
    assert c_vol is not None and c_vol.volume_id == SYSTEM_VOL and c_vol.page_file and "C:\\" in c_vol.mount_paths
    assert by["7"].serial is None and by["7"].unique_id  # virtual disk: identity from UniqueId only


def test_real_system_disk_is_unsuitable_even_if_every_bus_is_allowed(host_disks) -> None:
    classes, problems = classify_all(host_disks, ALL_BUSES, protected_for(), inspector=FakeInspector())
    assert problems == []
    sysd = next(c for c in classes if c.disk.disk_id == "2")
    assert sysd.drive_class is DriveClass.UNSUITABLE
    assert {"system_disk", "page_file"} <= set(sysd.reasons)
    assert not sysd.can_prepare and sysd.alert_code == "M-004"
    assert "used by the computer itself" in sysd.message


def test_real_system_disk_unsuitable_by_identity_without_os_flags(host_disks) -> None:
    """Even if Windows' IsSystem/IsBoot flags and the page file were absent, the volume GUID of
    the OS path alone excludes the disk."""
    d = next(x for x in host_disks if x.disk_id == "2")
    stripped = replace(d, is_system=False, is_boot=False, partitions=tuple(
        replace(p, is_system=False, is_boot=False, volume=replace(p.volume, page_file=False) if p.volume else None)
        for p in d.partitions))
    c = classify(stripped, ALL_BUSES, protected_for(), inspector=FakeInspector())
    assert c.drive_class is DriveClass.UNSUITABLE and "system_disk" in c.reasons


def test_real_internal_disks_are_not_offered_by_default(host_disks) -> None:
    classes, _ = classify_all(host_disks, POL, protected_for(), inspector=FakeInspector())
    assert all(c.drive_class is DriveClass.UNSUITABLE for c in classes)
    assert all("not_external" in c.reasons for c in classes)


def test_real_spool_disk_excluded_by_volume_identity_not_letter(host_disks) -> None:
    spool_vol = next(x for x in host_disks if x.disk_id == "4").partitions[0].volume.volume_id  # F: on the host
    prot = protected_for(spool_vol=spool_vol)
    # Remove the drive letter: protection must not depend on it.
    d4 = next(x for x in host_disks if x.disk_id == "4")
    p = d4.partitions[0]
    no_letter = replace(d4, partitions=(replace(p, volume=replace(p.volume, mount_paths=(spool_vol,))),))
    c = classify(no_letter, ALL_BUSES, prot, inspector=FakeInspector())
    assert c.drive_class is DriveClass.UNSUITABLE and "recording_storage" in c.reasons
    assert c.protected_roles == (ROLE_SPOOL,)
    assert "holds this computer's recordings" in c.message


def test_real_too_small_virtual_disk(host_disks) -> None:
    c = classify(next(x for x in host_disks if x.disk_id == "7"), ALL_BUSES.__class__(
        allowed_bus_types=ALL_BUSES.allowed_bus_types), protected_for(), inspector=FakeInspector())
    assert c.drive_class is DriveClass.UNSUITABLE and "too_small" in c.reasons


def test_real_data_disks_with_files_are_foreign(host_disks) -> None:
    files = RootContents(readable=True, entries=("Documents",))
    insp = FakeInspector({v.volume_id: files for d in host_disks for v in d.volumes})
    classes, _ = classify_all(host_disks, ALL_BUSES, protected_for(), inspector=insp)
    by = {c.disk.disk_id: c for c in classes}
    assert by["3"].drive_class is DriveClass.FOREIGN_DATA
    assert by["3"].needs_admin and by["3"].needs_serial
    assert "contains other files" in by["3"].message and "TB" in by["3"].message


# ---------------------------------------------------------------------------- synthetic classes

def test_blank_raw_disk() -> None:
    c = classify(blank_disk(), POL, protected_for(), inspector=FakeInspector())
    assert c.drive_class is DriveClass.BLANK and c.reasons == ("no_partition_table",)
    assert c.can_prepare and not c.needs_admin and not c.needs_serial
    assert c.message == "This drive is empty and needs to be prepared." and c.alert_code == "M-001"


def test_blank_gpt_with_only_msr() -> None:
    c = classify(usb_disk(partitions=(msr_part(),)), POL, protected_for(), inspector=FakeInspector())
    assert c.drive_class is DriveClass.BLANK and c.reasons == ("no_data_partitions",)


def test_empty_ntfs_without_marker_is_blank() -> None:
    d = usb_disk(partitions=(msr_part(), data_part(2, vol("00000000-0000-0000-0000-0000000000e1"))))
    c = classify(d, POL, protected_for(), inspector=FakeInspector())
    assert c.drive_class is DriveClass.BLANK and c.reasons == ("empty_volume",)


@pytest.mark.parametrize("fs,shown", [("exfat", "exFAT"), ("fat32", "FAT32"), ("ext4", "ext4"),
                                      ("hfsplus", "Mac (HFS+)")])
def test_wrong_format_empty(fs: str, shown: str) -> None:
    d = usb_disk(partitions=(data_part(1, vol("00000000-0000-0000-0000-0000000000e2", fs)),))
    c = classify(d, POL, protected_for(), inspector=FakeInspector())
    assert c.drive_class is DriveClass.WRONG_FORMAT
    assert c.message == f"This drive uses the wrong format ({shown}). It must be prepared before use."
    assert c.can_prepare and not c.needs_admin and c.alert_code == "M-002"


def test_approved_filesystem_is_configurable() -> None:
    exfat_pol = load_policy({"approved_filesystem": "exfat"})
    assert classify(exfat_disk(), exfat_pol, protected_for(), inspector=FakeInspector()).drive_class is DriveClass.BLANK
    ntfs_empty = usb_disk(partitions=(data_part(1, vol("00000000-0000-0000-0000-0000000000e3", "ntfs")),))
    c = classify(ntfs_empty, exfat_pol, protected_for(), inspector=FakeInspector())
    assert c.drive_class is DriveClass.WRONG_FORMAT and "(NTFS)" in c.message


def test_files_make_it_foreign() -> None:
    d = foreign_disk()
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=("DCIM", "notes.txt"))})
    c = classify(d, POL, protected_for(), inspector=insp)
    assert c.drive_class is DriveClass.FOREIGN_DATA
    assert c.message == "This drive contains other files (1 TB). Preparing it will erase them."
    assert c.needs_admin and c.needs_serial and c.can_prepare and c.alert_code == "M-003"


def test_os_created_root_entries_are_not_files() -> None:
    from sentinel.drives.marker import inspect_root

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as t:
        (Path(t) / "System Volume Information").mkdir()
        (Path(t) / "$RECYCLE.BIN").mkdir()
        rc = inspect_root(t)
        assert rc.readable and rc.entries == ()
        (Path(t) / "x.txt").write_text("x")
        assert inspect_root(t).entries == ("x.txt",)


@pytest.mark.parametrize("disk,reason", [
    (usb_disk(partitions=(data_part(1, vol("00000000-0000-0000-0000-0000000000e4", None)),)), "unreadable"),
    (usb_disk(partitions=(data_part(1, None),)), "unreadable"),
    (usb_disk(partitions=(PartitionInfo(1, "e1", GPT_EFI, KIND_EFI, 200 * 10**6),)), "os_partition"),
    (usb_disk(partitions=(PartitionInfo(1, "e2", "0fc63daf-8483-4772-8e79-3d69d8477de4", "other", 10**12),)),
     "unknown_partition"),
])
def test_uncertainty_is_foreign_never_blank(disk, reason: str) -> None:
    c = classify(disk, POL, protected_for(), inspector=FakeInspector())
    assert c.drive_class is DriveClass.FOREIGN_DATA and reason in c.reasons


def test_unlistable_volume_is_foreign() -> None:
    d = foreign_disk()
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=False, error="PermissionError")})
    assert classify(d, POL, protected_for(), inspector=insp).drive_class is DriveClass.FOREIGN_DATA


def test_ready_drive() -> None:
    d = usb_disk(partitions=(msr_part(), data_part(2, vol("00000000-0000-0000-0000-0000000000e5", label="SENTINEL_3"))))
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=("SENTINEL_DRIVE.json", "Sentinel"),
                                                                marker=MARKER)})
    c = classify(d, POL, protected_for(), inspector=insp)
    assert c.drive_class is DriveClass.READY and not c.can_prepare and c.refusal == "already_prepared"
    assert c.to_dict()["drive_uuid"] == MARKER["drive_uuid"] and c.alert_code is None


def test_sentinel_marker_on_unapproved_filesystem_is_foreign() -> None:
    d = exfat_disk()
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=("SENTINEL_DRIVE.json",),
                                                                marker=MARKER)})
    c = classify(d, POL, protected_for(), inspector=insp)
    assert c.drive_class is DriveClass.FOREIGN_DATA and "sentinel_drive_wrong_format" in c.reasons


@pytest.mark.parametrize("kw,reason", [
    ({"is_offline": True}, "offline"), ({"is_read_only": True}, "read_only"), ({"health": "Warning"}, "health"),
    ({"health": "Unhealthy"}, "health"), ({"size": 64 * 10**9}, "too_small"), ({"bus": "SATA"}, "not_external"),
    ({"serial": None, "unique_id": None}, "no_identity"), ({"is_system": True}, "system_disk"),
    ({"is_boot": True}, "system_disk"),
])
def test_unsuitable_reasons(kw: dict, reason: str) -> None:
    health = kw.pop("health", None)
    d = blank_disk(**kw)
    if health:
        d = replace(d, health=health)
    c = classify(d, POL, protected_for(), inspector=FakeInspector())
    assert c.drive_class is DriveClass.UNSUITABLE and reason in c.reasons and not c.can_prepare


def test_page_file_on_external_disk_is_unsuitable() -> None:
    d = usb_disk(partitions=(data_part(1, vol("00000000-0000-0000-0000-0000000000e6", page_file=True)),))
    assert "page_file" in classify(d, POL, protected_for(), inspector=FakeInspector()).reasons


def test_fail_closed_when_spool_location_unknown() -> None:
    prot = ProtectedStorage((ProtectedItem(ROLE_OS, "C:\\Windows", volume_id=SYSTEM_VOL),))
    classes, problems = classify_all([blank_disk()], POL, prot, inspector=FakeInspector())
    assert any("recordings" in p for p in problems)
    assert classes[0].drive_class is DriveClass.UNSUITABLE and classes[0].alert_code == "M-008"


def test_fail_closed_when_protected_volume_matches_no_disk() -> None:
    prot = protected_for(spool_vol="\\\\?\\Volume{deadbeef-0000-0000-0000-000000000000}\\")
    classes, problems = classify_all([blank_disk()], POL, prot, inspector=FakeInspector())
    assert classes[0].drive_class is DriveClass.UNSUITABLE and problems


def test_fail_closed_when_protected_path_unresolvable() -> None:
    prot = ProtectedStorage((ProtectedItem(ROLE_OS, "C:\\Windows", volume_id=SYSTEM_VOL),
                             ProtectedItem(ROLE_SPOOL, "\\\\server\\share", error="network drive")))
    classes, problems = classify_all([blank_disk()], POL, prot, inspector=FakeInspector())
    assert classes[0].drive_class is DriveClass.UNSUITABLE and any("network" in p for p in problems)


@pytest.mark.parametrize("mode,can,admin,serial", [("admin_only", True, True, True),
                                                   ("operator_with_serial", True, False, True),
                                                   ("never", False, False, False)])
def test_foreign_data_policy(mode: str, can: bool, admin: bool, serial: bool) -> None:
    d = foreign_disk()
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=("a",))})
    c = classify(d, load_policy({"foreign_data_authorization": mode}), protected_for(), inspector=insp)
    assert (c.can_prepare, c.needs_admin, c.needs_serial) == (can, admin, serial)
    if mode == "never":
        assert c.refusal == "policy_never"


# ---------------------------------------------------------------------------- policy / model

def test_policy_defaults_and_validation() -> None:
    p = load_policy(None)
    assert (p.approved_filesystem, p.foreign_data_authorization, p.allowed_bus_types) == ("ntfs", "admin_only", ("USB",))
    for bad in ({"approved_filesystem": "fat32"}, {"foreign_data_authorization": "anyone"}, {"nope": 1},
                {"min_size_gb": 0}, {"label_prefix": "bad prefix"}, {"serial_confirm_chars": 2},
                {"allowed_bus_types": []}):
        with pytest.raises(DrivePolicyError):
            load_policy(bad)
    assert load_policy({"approved_filesystem": "exfat"}).label_for(99) == "SENTINEL_99"
    with pytest.raises(DrivePolicyError):  # exFAT labels are limited to 11 characters
        load_policy({"approved_filesystem": "exfat"}).label_for(100)


def test_serial_normalisation_and_tail() -> None:
    assert norm_serial("            Z505GDE8") == "Z505GDE8"
    assert norm_serial("6479_A777_3000_0009.") == "6479_A777_3000_0009"
    assert serial_tail("6479_A777_3000_0009") == "0009"
    assert serial_tail("wd-wcc4e1234abcd") == "ABCD"
    assert serial_tail("ab") is None and serial_tail(None) is None


def test_identity_excludes_locators() -> None:
    a = blank_disk(disk_id="3")
    b = replace(a, disk_id="5", device_path="\\\\?\\other", location="Port 9")
    assert a.identity.fingerprint() == b.identity.fingerprint()
    c = replace(a, serial="OTHER")
    assert a.identity.fingerprint() != c.identity.fingerprint() and "serial" in a.identity.differences(c.identity)


# ---------------------------------------------------------------------------- Linux (lsblk)

def test_linux_real_wsl_capture() -> None:
    disks = parse_lsblk(load_fixture("linux_wsl_lsblk.json"))
    by = {d.disk_id: d for d in disks}
    assert set(by) == {"sda", "sdb", "sdc", "sdd"}
    assert by["sda"].is_read_only and by["sda"].unique_id == "0x60022480728b63b9f0b8d5e28f1d4859"
    assert by["sdc"].volumes[0].page_file                      # active swap
    assert by["sdd"].volumes[0].mount_paths == ("/mnt/wslg/distro", "/")
    prot = ProtectedStorage((ProtectedItem(ROLE_OS, "/", os_device="8:48"),
                             ProtectedItem(ROLE_SPOOL, "/var/lib/sentinel", os_device="8:48")))
    classes, problems = classify_all(disks, load_policy({"allowed_bus_types": ["UNKNOWN"], "min_size_gb": 1}), prot,
                                     inspector=FakeInspector())
    assert problems == []
    cls = {c.disk.disk_id: c for c in classes}
    assert cls["sdd"].drive_class is DriveClass.UNSUITABLE and "system_disk" in cls["sdd"].reasons
    assert "page_file" in cls["sdc"].reasons
    assert "read_only" in cls["sda"].reasons


def test_linux_constructed_usb_fixture() -> None:
    disks = parse_lsblk(load_fixture("linux_usb_constructed.json"))
    by = {d.disk_id: d for d in disks}
    assert set(by) == {"nvme0n1", "sda", "sdb", "sdc"}          # rom skipped
    assert "252:0" in by["nvme0n1"].os_devices                  # / is on LUKS inside nvme0n1p2
    prot = ProtectedStorage((ProtectedItem(ROLE_OS, "/", os_device="252:0"),
                             ProtectedItem(ROLE_SPOOL, "/var/lib/sentinel/spool", os_device="8:33")))
    files = RootContents(readable=True, entries=("DCIM",))
    insp = FakeInspector({by["sda"].volumes[0].volume_id: files})
    classes, problems = classify_all(disks, POL, prot, inspector=insp)
    assert problems == []
    cls = {c.disk.disk_id: c for c in classes}
    assert cls["nvme0n1"].drive_class is DriveClass.UNSUITABLE and "system_disk" in cls["nvme0n1"].reasons
    assert cls["sdc"].drive_class is DriveClass.UNSUITABLE and "recording_storage" in cls["sdc"].reasons
    assert cls["sda"].drive_class is DriveClass.FOREIGN_DATA and "412 GB" in cls["sda"].message
    assert cls["sdb"].drive_class is DriveClass.BLANK
    assert by["sda"].bus_type == "USB" and by["sda"].serial == "WD-WCC7K1234567"


def test_linux_unmounted_filesystem_is_not_assumed_empty() -> None:
    raw = load_fixture("linux_usb_constructed.json")
    sda = next(d for d in raw["blockdevices"] if d["name"] == "sda")
    sda["children"][0]["mountpoints"] = [None]
    disks = parse_lsblk(raw)
    d = next(x for x in disks if x.disk_id == "sda")
    from sentinel.drives.marker import inspect_volume

    c = classify(d, POL, protected_for(), inspector=inspect_volume)
    assert c.drive_class is DriveClass.FOREIGN_DATA and "unreadable" in c.reasons
