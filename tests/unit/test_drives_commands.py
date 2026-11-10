"""Generation of the destructive scripts. Scripts are inspected and *parsed*, never executed."""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from drive_fakes import SYSTEM_VOL, blank_disk, data_part, exfat_disk, system_disk, usb_disk, vol

from sentinel.alerts import CATALOG, Severity, make_alert
from sentinel.drives.commands import build_linux_script, build_windows_plan, build_windows_script
from sentinel.drives.errors import PrepareRefused
from sentinel.drives.model import DiskInfo
from sentinel.drives.prepare import WindowsUacRunner, parse_linux_result

WIN_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows PowerShell")


def _script(d: DiskInfo | None = None, **kw) -> str:
    d = d or exfat_disk()
    args = {"filesystem": "ntfs", "label": "SENTINEL_7", "protected_volume_ids": (SYSTEM_VOL,),
            "result_path": "C:\\tmp\\r.json"}
    args.update(kw)
    return build_windows_script(d, **args)


def _expected(script: str) -> dict:
    b64 = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script).group(1)
    return json.loads(base64.b64decode(b64).decode("utf-8"))


def test_windows_script_uses_verified_cmdlets_in_order() -> None:
    s = _script()
    order = ["Assert-Disk $d $true", "Clear-Disk -InputObject $d -RemoveData -RemoveOEM -Confirm:$false",
             "Initialize-Disk -InputObject $d -PartitionStyle GPT -Confirm:$false",
             "New-Partition -InputObject $d -UseMaximumSize\n",
             "Format-Volume -Partition $p -FileSystem $exp.filesystem -NewFileSystemLabel $exp.label -Confirm:$false -Force",
             "Add-PartitionAccessPath -InputObject $p -AssignDriveLetter"]
    pos = [s.index(x) for x in order]
    assert pos == sorted(pos), "identity check must come before the destructive cmdlets, in this order"
    assert "New-Partition" in s and not re.search(r"New-Partition[^\n]*-Confirm", s)  # New-Partition has no -Confirm
    assert s.count("Assert-Disk $d") == 3  # before clear, before initialize, before partition


def test_windows_script_embeds_identity_as_data() -> None:
    d = exfat_disk()
    exp = _expected(_script(d))
    v = d.volumes[0]
    assert exp == {
        "disk_number": 9, "serial": d.serial, "unique_id": d.unique_id, "model": d.model, "size": str(d.size_bytes),
        "partition_style": "GPT", "disk_guid": d.disk_guid, "mbr_signature": "",
        "partition_guids": list(d.identity.partition_guids), "partition_layout": [f"135266304:{v.size_bytes}"],
        "volume_ids": [v.volume_id.lower()], "fs_serials": [v.fs_serial],
        "protected_volumes": [SYSTEM_VOL.lower()], "filesystem": "NTFS", "label": "SENTINEL_7",
        "result_path": "C:\\tmp\\r.json"}


def test_hostile_hardware_strings_are_not_code() -> None:
    d = usb_disk(serial="X'; Remove-Item C:\\ -Recurse; '", model="M\u2019odel $(calc)")
    s = _script(d)
    assert "Remove-Item" not in s and "calc" not in s
    assert _expected(s)["serial"] == d.serial


def test_exfat_and_raw_disk() -> None:
    assert _expected(_script(filesystem="exfat", label="SENTINEL_9"))["filesystem"] == "exFAT"
    s = _script(blank_disk())
    e = _expected(s)
    assert e["disk_guid"] == "" and e["partition_guids"] == [] and e["partition_layout"] == []
    assert e["partition_style"] == "RAW"
    assert "if ((N $d.PartitionStyle) -ne 'RAW')" in s  # Clear-Disk is skipped on an uninitialised disk


@pytest.mark.parametrize("disk,kw,reason", [
    (system_disk(), {}, "system_disk"),
    (replace(blank_disk(), is_boot=True), {}, "system_disk"),
    (usb_disk(partitions=(data_part(1, vol("00000000-0000-0000-0000-0000000000f1", page_file=True)),)), {}, "page_file"),
    (exfat_disk(), {"protected_volume_ids": ("\\\\?\\Volume{c0ffee00-0000-0000-0000-000000000001}\\",)},
     "recording_storage"),
    (exfat_disk(), {"filesystem": "ext4"}, "filesystem_unsupported"),
    (exfat_disk(), {"label": "bad label"}, "bad_label"),
    (blank_disk(serial=None, unique_id=None), {}, "no_identity"),
])
def test_windows_generation_guards(disk: DiskInfo, kw: dict, reason: str) -> None:
    with pytest.raises(PrepareRefused) as ei:
        _script(disk, **kw)
    assert ei.value.reason == reason


def test_uac_launcher_argv() -> None:
    plan = build_windows_plan(exfat_disk(), filesystem="ntfs", label="SENTINEL_7", drive_uuid="u",
                              protected_volume_ids=(SYSTEM_VOL,), result_path="C:\\tmp\\r.json")
    argv = WindowsUacRunner().launcher_argv(plan) if sys.platform == "win32" else None
    if argv is None:
        pytest.skip("powershell path only resolvable on Windows")
    cmd = argv[-1]
    assert argv[-2] == "-Command" and "-Verb RunAs -Wait -PassThru" in cmd and "exit 1223" in cmd
    inner = re.search(r"-EncodedCommand ([A-Za-z0-9+/=]+)'", cmd).group(1)
    assert base64.b64decode(inner).decode("utf-16-le") == plan.script
    assert len(subprocess.list2cmdline(argv)) < 32_000  # Windows command-line limit 32,767


@WIN_ONLY
def test_generated_script_parses_and_every_cmdlet_and_parameter_exists() -> None:
    """Parse (do NOT run) the generated script with the real PowerShell parser, then check that every
    command it calls exists on this machine and that every named parameter exists on that command."""
    script = _script()
    checker = r"""
$ErrorActionPreference = 'Stop'
$text = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('__S__'))
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)
$funcs = @($ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) | ForEach-Object { $_.Name })
$out = @()
foreach ($c in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true)) {
    $name = $c.GetCommandName()
    if ($funcs -contains $name) { continue }
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    $params = @($c.CommandElements | Where-Object { $_ -is [System.Management.Automation.Language.CommandParameterAst] } | ForEach-Object { $_.ParameterName })
    $missing = @($params | Where-Object { -not $cmd -or -not $cmd.Parameters.ContainsKey($_) })
    $out += [pscustomobject]@{ name = $name; found = [bool]$cmd; missing = $missing }
}
[pscustomobject]@{ errors = @($errors | ForEach-Object { $_.Message }); commands = $out } | ConvertTo-Json -Depth 4 -Compress
""".replace("__S__", base64.b64encode(script.encode("utf-8")).decode("ascii"))
    from sentinel.drives.powershell import run_powershell

    res = json.loads(run_powershell(checker))
    assert res["errors"] in ([], None)
    cmds = res["commands"] if isinstance(res["commands"], list) else [res["commands"]]
    names = {c["name"] for c in cmds}
    assert {"Get-Disk", "Clear-Disk", "Initialize-Disk", "New-Partition", "Format-Volume", "Get-Partition",
            "Add-PartitionAccessPath",
            "Get-CimInstance", "Set-Content", "ConvertFrom-Json", "ConvertTo-Json"} <= names
    bad = [c for c in cmds if not c["found"] or c["missing"]]
    assert bad == []


@WIN_ONLY
def test_enumeration_script_parses() -> None:
    from sentinel.drives.windows_enum import ENUM_SCRIPT

    checker = (r"$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseInput("
               r"[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('"
               + base64.b64encode(ENUM_SCRIPT.encode()).decode() + r"')),[ref]$t,[ref]$e); $e.Count")
    from sentinel.drives.powershell import run_powershell

    assert run_powershell(checker).strip() == "0"


# ---------------------------------------------------------------------------- Linux

def _linux_disk(**kw) -> DiskInfo:
    base = dict(platform="linux", disk_id="sdb", device_path="/dev/sdb", os_devices=("8:16",))
    base.update(kw)
    return replace(blank_disk(), **base)


def test_linux_script_contents() -> None:
    d = _linux_disk(serial="ZA1B'2C3D")
    s = build_linux_script(d, filesystem="ntfs", label="SENTINEL_2", protected_devices=("252:0", "8:33"))
    order = ["check\n", "wipefs --all", "sfdisk --quiet --lock --wipe always --wipe-partitions always",
             "mkfs.ntfs --fast --label SENTINEL_2"]
    pos = [s.index(x) for x in order]
    assert pos == sorted(pos)
    assert "'ZA1B'\"'\"'2C3D'" in s  # shlex-quoted
    assert "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7" in s
    assert "for p in 252:0 8:33; do" in s
    ext4 = build_linux_script(d, filesystem="ext4", label="SENTINEL_2", protected_devices=())
    assert "mkfs.ext4 -F -L SENTINEL_2" in ext4 and "printf 'label: gpt\\n,,%s\\n' L" in ext4


def test_linux_script_refuses_protected_and_odd_devices() -> None:
    with pytest.raises(PrepareRefused):
        build_linux_script(_linux_disk(), filesystem="ntfs", label="SENTINEL_2", protected_devices=("8:16",))
    with pytest.raises(PrepareRefused):
        build_linux_script(_linux_disk(device_path="/dev/sdb; rm -rf /"), filesystem="ntfs", label="SENTINEL_2",
                           protected_devices=())


def _posix_sh() -> str | None:
    for c in (r"C:\Program Files\Git\bin\sh.exe", shutil.which("sh") if sys.platform != "win32" else None):
        if c and Path(c).exists():
            return c
    return None


@pytest.mark.skipif(_posix_sh() is None, reason="no POSIX sh for a syntax check")
def test_linux_script_is_valid_sh_syntax() -> None:
    s = build_linux_script(_linux_disk(), filesystem="exfat", label="SENTINEL_2", protected_devices=("252:0",))
    p = subprocess.run([_posix_sh(), "-n"], input=s.encode(), capture_output=True, timeout=30)
    assert p.returncode == 0, p.stderr.decode(errors="replace")


def test_parse_linux_result() -> None:
    assert parse_linux_result("x\nRESULT ok=1 step=done code=0 part=/dev/sdb1\n") == {
        "ok": True, "step": "done", "error": None, "partition": "/dev/sdb1"}
    assert parse_linux_result("RESULT ok=0 step=verify code=3 error=IDENTITY_MISMATCH_serial")["ok"] is False
    assert parse_linux_result("nothing") is None


# ---------------------------------------------------------------------------- alerts

def test_drive_alert_codes_present_and_plain() -> None:
    codes = [c for c in CATALOG if c.startswith("M-")]
    assert codes == [f"M-{i:03d}" for i in range(1, 11)]
    assert "D-001" in CATALOG and CATALOG["D-001"].headline == "Recording database problem"  # unchanged
    for c in codes:
        d = CATALOG[c]
        assert not d.stops_recording
        text = d.headline + d.action
        assert not re.search(r"[\\/]|NTFS|exFAT|PowerShell|Clear-Disk|\.json", text), c
        msg = make_alert(c, Severity.WARNING).message
        assert msg.startswith(f"Error {c}: ")
