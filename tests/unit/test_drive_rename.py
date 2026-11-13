"""Owner request 2026-11-12: each drive carries a sticker with a permanent name (SSD_1, SSD_2, …).
Drives are numbered SSD_<n> from now on, and a Sentinel drive can be renamed in place (its recordings
and identity stay; the rename is recorded)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from sentinel.db import Ledger
from sentinel.drives import cli as drives_cli
from sentinel.drives import rename as rn
from sentinel.drives.marker import MARKER_NAME, write_marker
from sentinel.drives.policy import DrivePolicy
from sentinel.drives.prepare import AUDIT_CATEGORY, next_label
from sentinel.ledgerchain import verify_ledger


def _drive(tmp: Path, name: str, label: str, led: Ledger) -> tuple[Path, str]:
    root = tmp / name
    root.mkdir()
    uid = str(uuid.uuid4())
    write_marker(root, {"format": "sentinel-drive", "version": 1, "drive_uuid": uid, "label": label,
                        "prepared_utc": "2026-11-12T10:00:00Z", "prepared_by": "tech", "role": "archive"})
    led.add_event("audit", AUDIT_CATEGORY, "drive_prepared", f"Drive prepared as {label}",
                  {"label": label, "drive_uuid": uid})
    return root, uid


@pytest.fixture()
def led(tmp_path: Path):  # noqa: ANN201
    ledger = Ledger(tmp_path / "sentinel.db")
    yield ledger
    ledger.close()


def _marker(root: Path) -> dict:
    return json.loads((root / MARKER_NAME).read_text("utf-8"))


def test_new_drives_are_numbered_ssd() -> None:
    assert DrivePolicy().label_for(1) == "SSD_1"


def test_renaming_keeps_the_drive_and_records_it(tmp_path: Path, led: Ledger) -> None:
    _drive(tmp_path, "dock1", "SENTINEL_1", led)
    _drive(tmp_path, "dock2", "SENTINEL_2", led)
    t7, uid = _drive(tmp_path, "t7", "SENTINEL_3", led)
    calls: list[tuple[str, str]] = []
    detail = rn.rename_drive(t7, "ssd_1", "agent7", led, DrivePolicy(), set_label=lambda r, n: calls.append((r, n)))
    assert calls == [(str(t7), "SSD_1")]
    m = _marker(t7)
    assert m["label"] == "SSD_1" and m["drive_uuid"] == uid and m["previous_labels"] == ["SENTINEL_3"]
    assert m["renamed_by"] == "agent7" and m["role"] == "archive"
    assert detail["old_label"] == "SENTINEL_3" and detail["new_label"] == "SSD_1"
    ev = led.query("SELECT detail FROM events WHERE code='drive_renamed'")
    assert json.loads(ev[0][0])["drive_uuid"] == uid
    assert rn.current_names(led)[uid] == "SSD_1"
    # The next new drive does not get SSD_1 again, even with the renamed drive unplugged.
    assert next_label(DrivePolicy(), led, []) == "SSD_2"
    assert verify_ledger(tmp_path / "sentinel.db").ok


@pytest.mark.parametrize("name", ["", "1SSD", "SSD 1", "SSD-1", "A" * 33])
def test_a_bad_name_changes_nothing(tmp_path: Path, led: Ledger, name: str) -> None:
    t7, _ = _drive(tmp_path, "t7", "SENTINEL_3", led)
    before = _marker(t7)
    with pytest.raises(rn.RenameRefused, match="cannot be a drive name"):
        rn.rename_drive(t7, name, "agent7", led, DrivePolicy(), set_label=lambda *_a: pytest.fail("label changed"))
    assert _marker(t7) == before


def test_a_name_another_drive_has_is_refused(tmp_path: Path, led: Ledger) -> None:
    _drive(tmp_path, "dock1", "SENTINEL_1", led)
    t7, _ = _drive(tmp_path, "t7", "SENTINEL_3", led)
    t7b, _ = _drive(tmp_path, "t7b", "SENTINEL_4", led)
    no = lambda *_a: pytest.fail("label changed")  # noqa: E731
    with pytest.raises(rn.RenameRefused, match="records is already called SENTINEL_1"):
        rn.rename_drive(t7, "SENTINEL_1", "agent7", led, DrivePolicy(), set_label=no)
    rn.rename_drive(t7, "SSD_1", "agent7", led, DrivePolicy(), set_label=lambda *_a: None)
    with pytest.raises(rn.RenameRefused, match="records is already called SSD_1"):
        rn.rename_drive(t7b, "SSD_1", "agent7", led, DrivePolicy(), set_label=no)
    with pytest.raises(rn.RenameRefused, match="connected drive is already called SSD_2"):
        rn.rename_drive(t7b, "SSD_2", "agent7", led, DrivePolicy(), set_label=no, other_labels=["ssd_2"])
    with pytest.raises(rn.RenameRefused, match="already called SSD_1"):
        rn.rename_drive(t7, "SSD_1", "agent7", led, DrivePolicy(), set_label=no)
    # The old name of a renamed drive is free again.
    rn.rename_drive(t7b, "SENTINEL_3", "agent7", led, DrivePolicy(), set_label=lambda *_a: None)


def test_a_drive_sentinel_did_not_set_up_is_refused(tmp_path: Path, led: Ledger) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(rn.RenameRefused, match="has not set up this drive"):
        rn.rename_drive(plain, "SSD_1", "agent7", led, DrivePolicy(), set_label=lambda *_a: None)


def test_when_windows_refuses_or_the_marker_fails_the_drive_is_left_as_it_was(tmp_path: Path, led: Ledger,
                                                                            monkeypatch) -> None:  # noqa: ANN001
    t7, _ = _drive(tmp_path, "t7", "SENTINEL_3", led)
    before = _marker(t7)

    def refuse(_r: str, _n: str) -> None:
        raise OSError(5, "access denied")
    with pytest.raises(OSError):
        rn.rename_drive(t7, "SSD_1", "agent7", led, DrivePolicy(), set_label=refuse)
    assert _marker(t7) == before
    labels: list[str] = []

    def broken(_root: Path, _m: dict) -> None:
        raise OSError("disk went away")
    monkeypatch.setattr(rn, "_write_marker", broken)
    with pytest.raises(OSError):
        rn.rename_drive(t7, "SSD_1", "agent7", led, DrivePolicy(), set_label=lambda _r, n: labels.append(n))
    assert labels == ["SSD_1", "SENTINEL_3"]              # volume name put back
    assert _marker(t7) == before
    assert not led.query("SELECT 1 FROM events WHERE code='drive_renamed'")


def test_the_command_needs_an_administrator(tmp_path: Path, monkeypatch, capsys) -> None:  # noqa: ANN001
    import sentinel.drives.prepare as prep

    monkeypatch.setattr(prep, "os_administrator_check", lambda: (False, "test"))
    env = type("E", (), {"cfg": object(), "windows": True})()
    args = type("A", (), {"drive": "E:", "name": "SSD_1", "who": "x"})()
    assert drives_cli.cmd_rename(env, args) == drives_cli.EXIT_REFUSED   # type: ignore[arg-type]
    assert "needs an administrator. Nothing was changed." in capsys.readouterr().out
