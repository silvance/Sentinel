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
