"""Docks that hide the disk's serial (field report 2026-11-10: a Sabrent EC-HD2B dual-bay dock reports
'000000000000000000' for every disk). A placeholder serial must not be confirmable by typing '0000',
and erasing is refused while another connected disk looks the same."""

from __future__ import annotations

import pytest
from drive_fakes import FakeInspector, blank_disk, foreign_disk, protected_for, system_disk

from sentinel.drives.classify import classify, classify_all
from sentinel.drives.errors import PrepareRefused
from sentinel.drives.marker import RootContents
from sentinel.drives.model import HIDDEN_SERIAL_WORD, DriveClass, confirm_text, serial_is_placeholder, serial_tail
from sentinel.drives.policy import DrivePolicy
from sentinel.drives.prepare import Authorization, Confirmation, check_guards

POL = DrivePolicy(erase_enabled=True)
ADMIN = Authorization("admin1", administrator=True, method="test")
ZEROS = "000000000000000000"
SYS = system_disk()


@pytest.mark.parametrize("s", [ZEROS, "FFFFFFFF", "  0000 0000 ", "0123456789AB", "123456789", "To Be Filled By O.E.M.",
                               "None", "----"])
def test_placeholders_are_recognised(s: str) -> None:
    assert serial_is_placeholder(s) and serial_tail(s) is None and confirm_text(s) == HIDDEN_SERIAL_WORD


@pytest.mark.parametrize("s", ["WD-WCC4E1234ABCD", "Z505GDE8", "6479_A777_3000_0009", "201206201434E200", "000000000001"])
def test_real_serials_are_not_placeholders(s: str) -> None:
    assert not serial_is_placeholder(s) and confirm_text(s) == serial_tail(s)


def test_absent_serial_is_not_a_placeholder() -> None:
    assert not serial_is_placeholder(None) and confirm_text(None) is None


def _foreign(**kw):
    d = foreign_disk(serial=ZEROS, unique_id=None, **kw)
    return d, FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=("x",))})


def test_hidden_serial_is_described_and_flagged() -> None:
    d, insp = _foreign()
    c = classify(d, POL, protected_for(), inspector=insp)
    assert c.drive_class is DriveClass.FOREIGN_DATA and c.can_prepare and c.needs_serial
    out = c.to_dict()
    assert out["serial_hidden"] is True and "hidden by the drive dock" in out["description"]


@pytest.mark.parametrize("typed,ok", [("0000", False), ("ERASE", True), ("erase", True), ("", False)])
def test_hidden_serial_is_confirmed_with_the_word_not_zeros(typed: str, ok: bool) -> None:
    d, insp = _foreign()
    c = classify(d, POL, protected_for(), inspector=insp)
    conf = Confirmation.for_classification(c, typed)
    if ok:
        check_guards(c, conf, ADMIN, POL)
    else:
        with pytest.raises(PrepareRefused) as e:
            check_guards(c, conf, ADMIN, POL)
        assert e.value.reason == "serial_mismatch"


def test_two_lookalike_disks_in_the_dock_cannot_be_erased() -> None:
    a = blank_disk(disk_id="5", serial=ZEROS, unique_id=None)
    b = blank_disk(disk_id="6", serial=ZEROS, unique_id=None)
    classes, _ = classify_all([a, b, SYS], POL, protected_for(), inspector=FakeInspector())
    classes = classes[:2]
    assert all(not c.can_prepare and c.refusal == "lookalike" for c in classes)
    assert "Remove the other drive" in classes[0].message
    with pytest.raises(PrepareRefused) as e:
        check_guards(classes[0], Confirmation.for_classification(classes[0], "ERASE"), ADMIN, POL)
    assert e.value.reason == "lookalike"


def test_one_disk_in_the_dock_can_be_erased_and_a_different_model_is_not_a_lookalike() -> None:
    a = blank_disk(disk_id="5", serial=ZEROS, unique_id=None)
    other = blank_disk(disk_id="6", serial=ZEROS, unique_id=None, model="Other disk")
    for disks in ([a, SYS], [a, other, SYS]):
        classes, _ = classify_all(disks, POL, protected_for(), inspector=FakeInspector())
        assert classes[0].can_prepare and classes[0].refusal is None


def test_lookalike_does_not_hide_other_refusals() -> None:
    a = blank_disk(disk_id="5", serial=ZEROS, unique_id=None)
    b = blank_disk(disk_id="6", serial=ZEROS, unique_id=None)
    off = DrivePolicy(erase_enabled=False)
    classes, _ = classify_all([a, b, SYS], off, protected_for(), inspector=FakeInspector())
    assert all(c.refusal == "erase_disabled" for c in classes[:2])


# ---------------------------------------------------------------------- field report 2026-11-11
def _known_dock_drive(model: str = "WDC WD40EFRX-68N", size: int = 4 * 10**12) -> "KnownDrives":
    """The ledger after SENTINEL_1 was set up in the dock: its serial was recorded as eighteen zeros."""
    from sentinel.drives.known import KnownDrives

    return KnownDrives(volume_ids=frozenset({"{c0ffee00-0000-0000-0000-00000000aaaa}"}),
                       hardware=frozenset({(ZEROS, model.lower(), size)}), serials=frozenset({ZEROS}),
                       unique_ids=frozenset({r"usbstor\disk&ven_dual&prod_sata\000000000000000000&0"}))


def test_a_brand_new_disk_in_the_dock_is_not_taken_for_the_archive_drive() -> None:
    """Sentinel had never seen the disk; it matched SENTINEL_1 only by the dock's placeholder serial."""
    new = blank_disk(disk_id="3", serial=ZEROS, unique_id=r"USBSTOR\DISK&VEN_DUAL&PROD_SATA\000000000000000000&0",
                     size=4 * 10**12)
    c = classify(new, POL, protected_for(), inspector=FakeInspector(), known=_known_dock_drive())
    assert c.drive_class is DriveClass.BLANK and c.can_prepare, (c.drive_class, c.reasons)


def test_a_known_drive_in_the_dock_is_still_recognised_by_its_contents() -> None:
    from drive_fakes import data_part, usb_disk, vol

    v = vol("c0ffee00-0000-0000-0000-00000000aaaa", "ntfs", label="SENTINEL_1")
    old = usb_disk(disk_id="4", serial=ZEROS, unique_id=None, partitions=(data_part(1, v),))
    c = classify(old, POL, protected_for(), inspector=FakeInspector(), known=_known_dock_drive())
    assert c.drive_class is DriveClass.SENTINEL_EVIDENCE and "known_volume" in c.reasons and not c.can_prepare


def test_a_real_serial_still_identifies_a_known_drive() -> None:
    from sentinel.drives.known import KnownDrives

    d = blank_disk(serial="WD-WCC4E1234ABCD")
    k = KnownDrives(hardware=frozenset({("wd-wcc4e1234abcd", d.model.lower(), d.size_bytes)}))
    assert classify(d, POL, protected_for(), inspector=FakeInspector(), known=k).drive_class \
        is DriveClass.SENTINEL_EVIDENCE


def test_a_new_disk_next_to_the_current_archive_drive_of_the_same_model_can_be_set_up() -> None:
    """Two bays, same model and placeholder serial: the archive drive has its own partition and volume
    IDs, so the two can be told apart; only two *empty* identical disks cannot."""
    from drive_fakes import data_part, usb_disk, vol

    v = vol("c0ffee00-0000-0000-0000-00000000bbbb", "ntfs", label="SENTINEL_1")
    archive = usb_disk(disk_id="4", serial=ZEROS, unique_id=None, partitions=(data_part(1, v),))
    new = blank_disk(disk_id="3", serial=ZEROS, unique_id=None)
    classes, _ = classify_all([archive, new, SYS], POL, protected_for(), inspector=FakeInspector())
    by = {c.disk.disk_id: c for c in classes}
    assert by["3"].can_prepare and by["3"].refusal is None


# Field report 2026-11-12: a brand-new, empty Samsung T7 (real serial) reported the disk UniqueId
# 5000000000000001, a USB bridge's filler value that the Sabrent dock's disks had reported too, and was
# refused as "holds Sentinel recordings" (reason known_hardware).
FILLER_UID = "5000000000000001"


@pytest.mark.parametrize("uid", [FILLER_UID, "6000000000000000000000000000000a", "naa.5000000000000001",
                                 "0000000000000000", "eui.0000000000000000", "FFFFFFFFFFFFFFFF", "None"])
def test_filler_unique_ids_are_placeholders(uid: str) -> None:
    from sentinel.drives.model import unique_id_is_placeholder

    assert unique_id_is_placeholder(uid)


@pytest.mark.parametrize("uid", [None, "", "5000C500B4074486", "500253F8C2B00399", "600224803426183AAFEC9E61EC7A093A",
                                 "eui.000000000000000000E04C70B6D755E0", "eui.6479A77730000009",
                                 r"USBSTOR\DISK&VEN_KINGSTON&PROD_DATATRAVELER_3.0&REV_0000\E0D55E6C09F21591D9ECAFF8&0:HOST"])
def test_real_unique_ids_are_not_placeholders(uid: str | None) -> None:
    from sentinel.drives.model import unique_id_is_placeholder

    assert not unique_id_is_placeholder(uid)


def _new_t7():
    from drive_fakes import data_part, usb_disk, vol

    v = vol("79fe323b-0000-0000-0000-100000000000", "exfat", label="T7")
    return usb_disk(disk_id="1", serial="V46972AY0LNNM7S", unique_id=FILLER_UID, model="PSSD T7",
                    size=2000398934016, partitions=(data_part(1, v),))


def test_a_new_ssd_sharing_the_docks_filler_unique_id_is_not_taken_for_a_sentinel_drive() -> None:
    from sentinel.drives.known import KnownDrives

    known = KnownDrives(unique_ids=frozenset({FILLER_UID}), serials=frozenset({ZEROS}),
                        hardware=frozenset({(ZEROS, "wdc wd40efrx-68n", 4 * 10**12)}))
    c = classify(_new_t7(), POL, protected_for(), inspector=FakeInspector(), known=known)
    assert c.drive_class is DriveClass.WRONG_FORMAT and c.can_prepare, (c.drive_class, c.reasons)


class _Audit:
    """Just enough of the ledger for load_known: drive_prepared events, no drives table."""

    def __init__(self, details: list[dict]) -> None:
        import json

        self.rows = [(json.dumps(d),) for d in details]

    def query(self, sql: str):
        return self.rows if "FROM events" in sql else []


def _ident(serial: str, uid: str | None) -> dict:
    return {"serial": serial, "unique_id": uid, "model": "WDC WD40EFRX-68N", "size_bytes": 4 * 10**12,
            "volume_ids": [], "fs_serials": []}


def test_the_ledger_does_not_keep_filler_or_dock_unique_ids() -> None:
    from sentinel.drives.known import load_known

    real = "5000C500B4074486"
    known = load_known(_Audit([
        {"drive_uuid": "11111111-1111-1111-1111-111111111111", "disk": _ident(ZEROS, FILLER_UID)},
        {"drive_uuid": "22222222-2222-2222-2222-222222222222",
         "disk": _ident(ZEROS, r"USBSTOR\DISK&VEN_DUAL&PROD_SATA\000000000000000000&0")},
        {"drive_uuid": "33333333-3333-3333-3333-333333333333", "identity_after": _ident("Z505GDE8", real)},
    ]))
    assert known.error is None and known.unique_ids == frozenset({real.lower()})
    c = classify(_new_t7(), POL, protected_for(), inspector=FakeInspector(), known=known)
    assert c.drive_class is DriveClass.WRONG_FORMAT, c.reasons


def test_once_set_up_the_ssd_is_still_recognised_by_its_real_serial() -> None:
    from sentinel.drives.known import KnownDrives

    t7 = _new_t7()
    k = KnownDrives(unique_ids=frozenset({FILLER_UID}),
                    hardware=frozenset({("v46972ay0lnnm7s", "pssd t7", t7.size_bytes)}))
    c = classify(t7, POL, protected_for(), inspector=FakeInspector(), known=k)
    assert c.drive_class is DriveClass.SENTINEL_EVIDENCE and c.reasons == ("known_hardware",)


def test_the_refusal_says_why_a_drive_counts_as_holding_recordings() -> None:
    from sentinel.drives.classify import evidence_why

    assert evidence_why(["known_hardware"]) == \
        "Sentinel's records show it set up a drive with this serial number before."
    assert evidence_why(["marker", "marker_invalid", "archive_layout"]) == \
        "It has Sentinel's drive file (SENTINEL_DRIVE.json). It has a folder named Sentinel at the top level."
    assert evidence_why(["something_new"]) == ""
