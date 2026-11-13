"""Regression tests for the independent QA review of design/drive-prepare (findings 1.1-1.6).

No real disk is touched; PowerShell is only used to evaluate a pure helper function.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from drive_fakes import (
    SYSTEM_VOL,
    FakeInspector,
    FakeRunner,
    Sequence,
    blank_disk,
    data_part,
    exfat_disk,
    foreign_disk,
    load_fixture,
    prepared_disk_like,
    protected_for,
    system_disk,
    usb_disk,
    vol,
)

from sentinel.alerts import CATALOG
from sentinel.config import ConfigError, load_config_dict
from sentinel.db import Ledger
from sentinel.drives import cli
from sentinel.drives.classify import (
    BLANK_MESSAGE,
    EVIDENCE_MESSAGE,
    UNSUITABLE_MESSAGES,
    classify,
    classify_all,
)
from sentinel.drives.commands import WINDOWS_TEMPLATE, build_linux_script, build_windows_script
from sentinel.drives.errors import IdentityChanged, PrepareFailed, PrepareRefused
from sentinel.drives.installed import installed_config_problems
from sentinel.drives.known import KnownDrives, load_known
from sentinel.drives.marker import MARKER_NAME, RootContents, inspect_root
from sentinel.drives.model import DriveClass
from sentinel.drives.policy import DrivePolicy, load_policy
from sentinel.drives.prepare import (
    EXIT_TIMEOUT,
    Authorization,
    Confirmation,
    HelperResult,
    PrepareContext,
    WindowsUacRunner,
    check_guards,
    prepare_drive,
    terminate_process,
)
from sentinel.drives.windows_enum import parse_windows_snapshot

ADMIN = Authorization("admin1", administrator=True, method="test")
OK = HelperResult(0, {"ok": True, "step": "done"})
SYS = system_disk()
POLICIES = ["admin_only", "operator_with_serial", "never"]
GOOD_MARKER = {"format": "sentinel-drive", "version": 1, "drive_uuid": "0b0e5f0a-1c2d-4e3f-8a9b-0c1d2e3f4a5b",
               "label": "SENTINEL_3", "prepared_utc": "2026-11-09T00:00:00.000000Z", "prepared_by": "admin1"}


@pytest.fixture()
def ledger(tmp_path: Path):
    led = Ledger(tmp_path / "data" / "sentinel.db")
    yield led
    led.close()


def _dir_inspector(mapping: dict[str, Path]):
    """Real inspect_root on temp directories standing in for volumes."""
    return lambda v: inspect_root(mapping[v.volume_id]) if v.volume_id in mapping else RootContents(readable=True)


# ============================================================================ 1.1 evidence drives

def _review_drive(root: Path) -> None:  # layout written by the export branch
    (root / "README_FIRST.txt").write_text("x")
    for d in ("Recordings", "Originals", "Manifests", "Reports"):
        (root / d).mkdir()
    (root / "Manifests" / "Export_Manifest.json").write_text("{}")


def _archive_drive(root: Path) -> None:
    (root / "Sentinel" / "INDEX").mkdir(parents=True)
    (root / "Sentinel" / "CAM01" / "2026-11-09").mkdir(parents=True)


LAYOUTS = {
    "review_no_marker": (_review_drive, None),
    "archive_no_marker": (_archive_drive, None),
    "corrupt_marker": (_archive_drive, "{not json"),
    "corrupt_marker_alone": (lambda r: None, "{not json"),
    "marker_missing_fields": (lambda r: None, json.dumps({"format": "sentinel-drive"})),
    "newer_version_marker": (_archive_drive, json.dumps({**GOOD_MARKER, "version": 2})),
    "partial_marker": (lambda r: (r / (MARKER_NAME + ".partial")).write_text("{}"), None),
    "only_manifest": (lambda r: ((r / "Manifests").mkdir(), (r / "Manifests" / "SHA256SUMS.txt").write_text("")), None),
    # Integration: the merged export branch writes Manifests/Export_<stamp>_<id8>/... (one level down).
    "only_export_manifest_subfolder": (lambda r: ((r / "Manifests" / "Export_20261109-140000_0123abcd").mkdir(parents=True),
                                                  (r / "Manifests" / "Export_20261109-140000_0123abcd"
                                                   / "Export_Manifest.json").write_text("{}")), None),
}


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_sentinel_layouts_are_evidence_under_every_policy(tmp_path: Path, layout: str, policy: str) -> None:
    make, marker_text = LAYOUTS[layout]
    root = tmp_path / "vol"
    root.mkdir()
    make(root)
    if marker_text is not None:
        (root / MARKER_NAME).write_text(marker_text, encoding="utf-8")
    d = foreign_disk()
    pol = load_policy({"erase_enabled": True, "foreign_data_authorization": policy})
    c = classify(d, pol, protected_for(), inspector=_dir_inspector({d.volumes[0].volume_id: root}))
    assert c.drive_class is DriveClass.SENTINEL_EVIDENCE, (layout, c.reasons)
    assert c.message == EVIDENCE_MESSAGE == "This drive holds Sentinel recordings and will not be erased. " \
                                            "Contact technical support."
    assert not c.can_prepare and c.refusal == "sentinel_evidence" and c.alert_code == "M-009"
    with pytest.raises(PrepareRefused) as ei:
        check_guards(c, Confirmation.for_classification(c, "ABCD"), ADMIN, pol)
    assert ei.value.reason == "sentinel_evidence" and ei.value.code == "M-009"


def test_valid_marker_on_wrong_filesystem_or_second_partition_is_evidence(tmp_path: Path) -> None:
    r1, r2 = tmp_path / "a", tmp_path / "b"
    for r in (r1, r2):
        r.mkdir()
    (r2 / MARKER_NAME).write_text(json.dumps(GOOD_MARKER), encoding="utf-8")
    two = usb_disk(partitions=(data_part(1, vol("00000000-0000-0000-0000-0000000000a1")),
                               data_part(2, vol("00000000-0000-0000-0000-0000000000a2"),
                                         guid="aaaaaaaa-0000-0000-0000-000000000002", offset=2 * 10**12)))
    insp = _dir_inspector({two.volumes[0].volume_id: r1, two.volumes[1].volume_id: r2})
    assert classify(two, DrivePolicy(erase_enabled=True), protected_for(), inspector=insp).drive_class is DriveClass.SENTINEL_EVIDENCE


def test_evidence_drive_never_reaches_the_helper(ledger: Ledger, tmp_path: Path) -> None:
    root = tmp_path / "vol"
    root.mkdir()
    _review_drive(root)
    d = foreign_disk()
    insp = _dir_inspector({d.volumes[0].volume_id: root})
    runner = FakeRunner(OK)
    ctx = PrepareContext(policy=load_policy({"erase_enabled": True, "foreign_data_authorization": "operator_with_serial"}),
                         protected=protected_for(), enumerate=Sequence([d, SYS]), runner=runner, audit=ledger,
                         platform="windows", inspector=insp, settle_s=0)
    forged = Confirmation(d.disk_id, d.identity.fingerprint(), DriveClass.FOREIGN_DATA, "ABCD")
    with pytest.raises(PrepareRefused) as ei:
        prepare_drive(ctx, d.disk_id, forged, ADMIN)
    assert ei.value.reason == "sentinel_evidence" and runner.plans == []


def _prepared_event(ledger: Ledger, d, **extra) -> None:
    ledger.add_event("audit", "drives", "drive_prepared", "t", {
        "drive_uuid": GOOD_MARKER["drive_uuid"], "volume_id": extra.get("volume_id"),
        "disk": d.identity.to_dict(), "identity_after": d.identity.to_dict(), "label": "SENTINEL_3"})


def test_drive_known_in_ledger_is_evidence_even_if_it_now_looks_blank(ledger: Ledger, tmp_path: Path) -> None:
    earlier = prepared_disk_like(blank_disk(), "SENTINEL_3")
    _prepared_event(ledger, earlier, volume_id=earlier.volumes[0].volume_id)
    known = load_known(ledger)
    wiped = blank_disk()  # same hardware, marker and partitions gone
    c = classify(wiped, DrivePolicy(erase_enabled=True), protected_for(), inspector=FakeInspector(), known=known)
    assert c.drive_class is DriveClass.SENTINEL_EVIDENCE and "known_hardware" in c.reasons
    # Same volume GUID / filesystem serial on different-looking hardware (e.g. other dock):
    moved = replace(earlier, serial="DOCK0000", unique_id=None)
    c2 = classify(moved, DrivePolicy(erase_enabled=True), protected_for(), inspector=FakeInspector(), known=known)
    assert {"known_volume", "known_filesystem"} <= set(c2.reasons)
    # ...and it stops prepare_drive, which re-reads the ledger itself:
    runner = FakeRunner(OK)
    ctx = PrepareContext(policy=DrivePolicy(erase_enabled=True), protected=protected_for(), enumerate=Sequence([wiped, SYS]),
                         runner=runner, audit=ledger, platform="windows", inspector=FakeInspector(), settle_s=0)
    with pytest.raises(PrepareRefused) as ei:
        prepare_drive(ctx, wiped.disk_id, Confirmation(wiped.disk_id, wiped.identity.fingerprint(), DriveClass.BLANK),
                      ADMIN)
    assert ei.value.reason == "sentinel_evidence" and runner.plans == []


def test_known_from_export_branch_drives_table(ledger: Ledger) -> None:
    # Integration: the export branch's `drives` table now exists in every ledger (db.EXPORT_DDL), so
    # the row goes into the real table instead of a stand-in schema created by the test.
    with ledger.tx() as c:
        c.execute("INSERT INTO drives (drive_id, drive_uuid, serial, identity, first_seen_utc, last_seen_utc) "
                  "VALUES ('drive_uuid:u-1', 'u-1', '  WD-WCC4E1234ABCD ', '{}', 't', 't')")
    known = load_known(ledger)
    assert "wd-wcc4e1234abcd" in known.serials
    c = classify(blank_disk(), DrivePolicy(erase_enabled=True), protected_for(), inspector=FakeInspector(), known=known)
    assert c.drive_class is DriveClass.SENTINEL_EVIDENCE


def _export_batch(c, drive_id: str, batch_id: str) -> None:
    c.execute("INSERT INTO export_batches (batch_id, purpose, camera_id, drive_id, state, interval_start_utc, "
              "interval_end_utc, display_timezone, app_version, stamp, planned_utc, updated_utc) "
              "VALUES (?, 'review', 'CAM01', ?, 'COMPLETE', 't', 't', 'UTC', '0', 's', 't', 't')", (batch_id, drive_id))


def test_export_drives_row_matches_by_volume_guid_and_volume_serial(ledger: Ledger) -> None:
    """Integration: export stores the bare `{GUID}` and an 8-hex-digit volume serial; drives
    enumerates `\\\\?\\Volume{GUID}\\` and a decimal serial. A review drive known only from an
    export (no SENTINEL_DRIVE.json, serial not visible) must still be recognised."""
    guid = "0f0e0d0c-1111-2222-3333-444455556666"
    d = usb_disk(serial="DOCK0000", unique_id=None, partitions=(data_part(1, vol(guid)),))
    dec_serial = d.volumes[0].fs_serial
    with ledger.tx() as c:
        c.execute("INSERT INTO drives (drive_id, volume_guid, identity, first_seen_utc, last_seen_utc) "
                  "VALUES (?, ?, '{}', 't', 't')", (f"volume_guid:{{{guid.upper()}}}", f"{{{guid.upper()}}}"))
        c.execute("INSERT INTO drives (drive_id, identity, first_seen_utc, last_seen_utc) VALUES ('x', ?, 't', 't')",
                  (json.dumps({"volume_serial": f"{int(dec_serial):08X}"}),))
        # Review 2026-11-12: only drives something was exported to count (see test_drives_review_1112_medium).
        for i, drive_id in enumerate((f"volume_guid:{{{guid.upper()}}}", "x")):
            _export_batch(c, drive_id, f"b{i}")
    known = load_known(ledger)
    assert {"known_volume", "known_filesystem"} <= set(known.matches(d))


def test_ready_drive_stays_ready_when_known(ledger: Ledger) -> None:
    d = prepared_disk_like(blank_disk(), "SENTINEL_3")
    _prepared_event(ledger, d, volume_id=d.volumes[0].volume_id)
    insp = FakeInspector({d.volumes[0].volume_id: RootContents(readable=True, entries=(MARKER_NAME, "Sentinel"),
                                                                marker=GOOD_MARKER,
                                                                sentinel_signs=("marker", "archive_layout"))})
    c = classify(d, DrivePolicy(erase_enabled=True), protected_for(), inspector=insp, known=load_known(ledger))
    assert c.drive_class is DriveClass.READY


def test_unreadable_ledger_fails_closed() -> None:
    class Broken:
        def query(self, *a, **k):
            raise RuntimeError("disk I/O error")

    known = load_known(Broken())
    assert known.error
    classes, problems = classify_all([blank_disk()], DrivePolicy(erase_enabled=True), protected_for(), inspector=FakeInspector(),
                                     known=known)
    assert classes[0].drive_class is DriveClass.UNSUITABLE and "ledger_unavailable" in classes[0].reasons
    assert classes[0].alert_code == "M-008" and any("ledger" in p for p in problems)


# ============================================================================ 1.2 stronger identity

def test_mbr_fixture_parses_signature_layout_and_serials() -> None:
    disks = {d.disk_id: d for d in parse_windows_snapshot(load_fixture("windows_mbr_usb_constructed.json"))}
    a, b = disks["6"], disks["8"]
    assert a.partition_style == b.partition_style == "MBR" and a.disk_guid is None
    assert (a.mbr_signature, b.mbr_signature) == ("3735928559", "305419896")
    assert a.identity.partition_layout == ("1048576:4000785104896",) and a.identity.fs_serials == ("1450500608",)
    assert a.identity_gaps() == []
    # Same model + bridge-reported serial "000000000000" (risk C2): hardware identity is identical...
    assert a.identity.same_device(b.identity)
    # ...but the full identity is not, so a swap during the UAC prompt is detected.
    assert a.identity.fingerprint() != b.identity.fingerprint()
    assert {"mbr_signature", "volume_ids", "fs_serials"} <= set(a.identity.differences(b.identity))


def test_identical_model_swap_with_dock_serial_refused(ledger: Ledger, tmp_path: Path) -> None:
    disks = {d.disk_id: d for d in parse_windows_snapshot(load_fixture("windows_mbr_usb_constructed.json"))}
    confirmed = disks["6"]
    swapped = replace(disks["8"], disk_id="6")  # other drive, same bay number after the swap
    files = RootContents(readable=True, entries=("x",))
    insp = FakeInspector({v.volume_id: files for d in disks.values() for v in d.volumes})
    runner = FakeRunner(OK)
    ctx = PrepareContext(policy=DrivePolicy(erase_enabled=True), protected=protected_for(), enumerate=Sequence([confirmed, SYS],
                                                                                              [swapped, SYS]),
                         runner=runner, audit=ledger, platform="windows", inspector=insp, settle_s=0)
    c = classify(confirmed, DrivePolicy(erase_enabled=True), protected_for(), inspector=insp)
    with pytest.raises(IdentityChanged):
        # The dock reports a placeholder serial: confirmed with the word, never "0000" (2026-11-10).
        prepare_drive(ctx, "6", Confirmation.for_classification(c, "ERASE"), ADMIN)
    assert runner.plans == []


def test_helper_compares_every_captured_identifier() -> None:
    disks = {d.disk_id: d for d in parse_windows_snapshot(load_fixture("windows_mbr_usb_constructed.json"))}
    s = build_windows_script(disks["6"], filesystem="ntfs", label="SENTINEL_1", protected_volume_ids=(SYSTEM_VOL,),
                             result_path="C:\\tmp\\r.json")
    for check in ("IDENTITY_MISMATCH partition_style", "IDENTITY_MISMATCH disk_guid", "IDENTITY_MISMATCH mbr_signature",
                  "IDENTITY_MISMATCH partition_guids", "IDENTITY_MISMATCH partition_layout",
                  "IDENTITY_MISMATCH volume_ids", "IDENTITY_MISMATCH fs_serials"):
        assert check in s
    exp = json.loads(base64.b64decode(re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", s).group(1)))
    assert exp["mbr_signature"] == "3735928559" and exp["partition_style"] == "MBR"
    assert exp["partition_layout"] == ["1048576:4000785104896"] and exp["fs_serials"] == ["1450500608"]
    assert s.index("Assert-Disk $d $true") < s.index("Clear-Disk")


@pytest.mark.parametrize("mutate,gap", [
    (lambda d: replace(d, mbr_signature=None), "mbr_signature"),
    (lambda d: replace(d, partition_style="GPT", disk_guid=None), "disk_guid"),
    (lambda d: replace(d, partitions=tuple(replace(p, offset=None) for p in d.partitions)), "partition_1_offset"),
    (lambda d: replace(d, partitions=tuple(replace(p, volume=replace(p.volume, fs_serial=None))
                                           for p in d.partitions)), "partition_1_fs_serial"),
])
def test_missing_captured_identifier_refuses(mutate, gap: str) -> None:
    disks = {d.disk_id: d for d in parse_windows_snapshot(load_fixture("windows_mbr_usb_constructed.json"))}
    d = mutate(disks["6"])
    assert gap in d.identity_gaps()
    with pytest.raises(PrepareRefused) as ei:
        build_windows_script(d, filesystem="ntfs", label="SENTINEL_1", protected_volume_ids=(), result_path="r")
    assert ei.value.reason == "identity_incomplete"
    c = classify(d, DrivePolicy(erase_enabled=True), protected_for(), inspector=FakeInspector())
    with pytest.raises(PrepareRefused) as ei2:
        check_guards(c, Confirmation.for_classification(c, "0000"), ADMIN, DrivePolicy(erase_enabled=True))
    assert ei2.value.reason == "identity_incomplete"


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows PowerShell")
def test_same_set_semantics_in_real_powershell() -> None:
    """Evaluate only the pure Same-Set function from the helper template (no disk cmdlets)."""
    fn = re.search(r"function Same-Set\(.*?\n\}\n", WINDOWS_TEMPLATE, re.S).group(0)
    cases = ("@((Same-Set @('a','b') @('b','a')), (Same-Set @('a') @('a','b')), (Same-Set @() @()), "
             "(Same-Set @('a','c') @('a','b')), (Same-Set @('a') @()), (Same-Set @('1048576:9') @('1048576:9'))) "
             "| ConvertTo-Json -Compress")
    from sentinel.drives.powershell import run_powershell

    assert json.loads(run_powershell(fn + cases)) == [True, False, True, False, False, True]


def test_linux_script_checks_partuuids_and_fs_uuids() -> None:
    d = replace(blank_disk(), platform="linux", disk_id="sdb", device_path="/dev/sdb", os_devices=("8:16",),
                partitions=exfat_disk().partitions)
    s = build_linux_script(d, filesystem="ntfs", label="SENTINEL_2", protected_devices=())
    assert "IDENTITY_MISMATCH_partuuid" in s and "IDENTITY_MISMATCH_fsuuid" in s
    assert "aaaaaaaa-0000-0000-0000-000000000001 " in s
    assert s.index("IDENTITY_MISMATCH_fsuuid") < s.index("wipefs --all")


# ============================================================================ 1.3 installed configuration

def _installed(tmp_path: Path, *, lock=True, ledger=True, spool=True) -> tuple[Path, object]:
    cfgfile = tmp_path / "ProgramData" / "Sentinel" / "sentinel.toml"
    cfgfile.parent.mkdir(parents=True)
    cfgfile.write_text("x")
    data = tmp_path / "data"
    data.mkdir()
    if spool:
        (tmp_path / "spool").mkdir()
    if ledger:
        Ledger(data / "sentinel.db").close()
    if lock:
        (data / "recorder.lock").write_text("")
    cfg = load_config_dict({"camera": {"id": "CAM01", "url": "tcp://127.0.0.1:9", "development_source": True},
                            "storage": {"spool_dir": "spool", "data_dir": "data", "log_dir": "logs"}},
                           base_dir=tmp_path)
    return cfgfile, cfg


def test_installed_config_accepted(tmp_path: Path) -> None:
    path, cfg = _installed(tmp_path)
    assert installed_config_problems(path, cfg, expected=path) == []


@pytest.mark.parametrize("kw,needle", [({"lock": False}, "never run"), ({"ledger": False}, "no recorder ledger"),
                                       ({"spool": False}, "recording storage folder")])
def test_installed_config_problems(tmp_path: Path, kw: dict, needle: str) -> None:
    path, cfg = _installed(tmp_path, **kw)
    assert any(needle in p for p in installed_config_problems(path, cfg, expected=path))


def test_other_config_file_is_refused(tmp_path: Path) -> None:
    path, cfg = _installed(tmp_path)
    copy = tmp_path / "copy.toml"
    copy.write_text("x")
    assert any("not the installed" in p for p in installed_config_problems(copy, cfg, expected=path))
    assert any("not the installed" in p for p in installed_config_problems(None, cfg, expected=path))


def test_cli_prepare_refuses_non_installed_config(tmp_path: Path, monkeypatch, capsys) -> None:
    from types import SimpleNamespace

    called = []
    monkeypatch.setattr(cli._installed, "installed_config_problems", lambda p, c: ["not the installed one"])
    monkeypatch.setattr("sentinel.drives.prepare.prepare_drive", lambda *a: called.append(a))

    class Env:
        policy, protected, windows = DrivePolicy(erase_enabled=True), protected_for(), True
        cfg = SimpleNamespace(data_path=tmp_path / "data")
        config_path = tmp_path / "x.toml"

        def enumerate(self):
            return [blank_disk(), SYS]

        def classify(self):
            return classify_all(self.enumerate(), self.policy, self.protected, inspector=FakeInspector())

    args = cli.build_parser().parse_args(["prepare", "--disk", "9", "--yes", "--serial-tail", "ABCD"])
    assert cli.cmd_prepare(Env(), args, interactive=False) == cli.EXIT_REFUSED and called == []
    assert "Error M-008" in capsys.readouterr().out


def test_installed_path_names() -> None:
    from sentinel.drives.installed import installed_config_path

    p = installed_config_path()
    # Integration: the installed path is the shared one from sentinel.paths (Windows: ...\Sentinel\config\).
    assert p.name == "sentinel.toml" and p.parent.name.lower() in ("sentinel", "config")
    if sys.platform == "win32":
        assert p.parent.name == "config" and p.parent.parent.name == "Sentinel"


# ============================================================================ 1.4 operator wording

def test_destructive_action_never_called_prepare_in_operator_text() -> None:
    texts = list(UNSUITABLE_MESSAGES.values()) + [BLANK_MESSAGE, EVIDENCE_MESSAGE]
    for d in (exfat_disk(), foreign_disk(), blank_disk()):
        insp = FakeInspector({v.volume_id: RootContents(readable=True, entries=("a",)) for v in d.volumes})
        texts.append(classify(d, DrivePolicy(erase_enabled=True), protected_for(), inspector=insp).message)
    texts += [CATALOG[c].headline + " " + CATALOG[c].action for c in CATALOG if c.startswith("M-")]
    for t in texts:
        assert "prepar" not in t.lower(), t
    assert "Erase and Set Up Drive" in CATALOG["M-001"].action and "Erase and Set Up Drive" in CATALOG["M-002"].action
    assert "Prepare a review drive" in CATALOG["S-001"].action  # the export wording is untouched


# ============================================================================ 1.5 config.py

def _raw(**drives) -> dict:
    d = {"camera": {"id": "CAM01", "url": "tcp://127.0.0.1:9", "development_source": True},
         "storage": {"spool_dir": "s", "data_dir": "d", "log_dir": "l"}}
    if drives:
        d["drives"] = drives
    return d


def test_config_accepts_and_validates_drives_section(tmp_path: Path) -> None:
    assert load_config_dict(_raw(), base_dir=tmp_path).drives == DrivePolicy()
    cfg = load_config_dict(_raw(approved_filesystem="exfat", foreign_data_authorization="never"), base_dir=tmp_path)
    assert cfg.drives.approved_filesystem == "exfat" and cfg.drives.foreign_data_authorization == "never"
    for bad in ({"approved_filesystem": "fat32"}, {"unknown_key": 1}, {"foreign_data_authorization": "anyone"}):
        with pytest.raises(ConfigError):
            load_config_dict(_raw(**bad), base_dir=tmp_path)
    with pytest.raises(ConfigError):
        load_config_dict({**_raw(), "nonsense": {}}, base_dir=tmp_path)  # other unknown sections still rejected


def test_example_config_still_loads() -> None:
    from sentinel.config import load_config

    example = Path(__file__).resolve().parents[2] / "config" / "sentinel.example.toml"
    assert load_config(example).drives == DrivePolicy()


# ============================================================================ 1.6 helper timeout

def test_uac_runner_timeout_reports_still_running(tmp_path: Path, monkeypatch) -> None:
    from sentinel.drives import prepare as prep
    from sentinel.drives.commands import build_windows_plan

    result = tmp_path / "r.json"
    plan = build_windows_plan(exfat_disk(), filesystem="ntfs", label="SENTINEL_1", drive_uuid="u",
                              protected_volume_ids=(SYSTEM_VOL,), result_path=str(result))

    def fake_run(*a, **k):
        result.write_text(json.dumps({"ok": False, "running": True, "step": "clear", "pid": 4242}), encoding="utf-8-sig")
        raise subprocess.TimeoutExpired("powershell", 1)

    monkeypatch.setattr(prep.subprocess, "run", fake_run)
    monkeypatch.setattr(prep, "terminate_process", lambda pid: (False, f"access denied terminating {pid}"))
    monkeypatch.setattr(WindowsUacRunner, "launcher_argv", lambda self, plan: ["x"])
    hr = WindowsUacRunner(timeout_s=1).run(plan)
    assert hr.exit_code == EXIT_TIMEOUT
    assert hr.result == {"ok": False, "step": "clear", "error": "timeout", "helper_pid": 4242, "still_running": True,
                         "terminate_error": "access denied terminating 4242"}


def test_still_running_helper_is_reported_as_possibly_erasing(ledger: Ledger, tmp_path: Path) -> None:
    d = blank_disk()
    hr = HelperResult(EXIT_TIMEOUT, {"ok": False, "step": "verify", "error": "timeout", "helper_pid": 4242,
                                     "still_running": True, "terminate_error": "access denied"})
    ctx = PrepareContext(policy=DrivePolicy(erase_enabled=True), protected=protected_for(), enumerate=Sequence([d, SYS]),
                         runner=FakeRunner(hr), audit=ledger, platform="windows", inspector=FakeInspector(),
                         settle_s=0, work_dir=tmp_path / "w")
    with pytest.raises(PrepareFailed) as ei:
        prepare_drive(ctx, d.disk_id, Confirmation(d.disk_id, d.identity.fingerprint(), DriveClass.BLANK), ADMIN)
    assert ei.value.reason == "helper_still_running" and "Do not unplug" in str(ei.value)
    ev = json.loads(ledger.query("SELECT detail FROM events WHERE code='drive_prepare_failed'")[0][0])
    assert ev["erase_started"] is True and ev["still_running"] is True and ev["helper_pid"] == 4242


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process API")
def test_terminate_process_never_kills_a_non_helper_process() -> None:
    ok, err = terminate_process(os.getpid())  # our own python.exe: image check must refuse
    assert not ok and "not the helper" in err
    ok, err = terminate_process(0x7FFFFFF0)    # no such process
    assert not ok and "cannot open process" in err
