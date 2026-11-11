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
