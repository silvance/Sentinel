"""Windows ACL helpers: SDDL parsing (any OS) and real DACL checks / icacls round trips (Windows).

The SDDL strings below were captured on the Windows 11 development host with
``ConvertSecurityDescriptorToStringSecurityDescriptorW`` (see sentinel.platform.winacl).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sentinel.platform import winacl
from sentinel.platform.winacl import (SID_ADMINISTRATORS, SID_SYSTEM, SID_USERS, disallowed_grants, effective_allow,
                                      parse_sddl, service_sid)

ME = "S-1-5-21-3365957325-2048722254-1525407219-1001"
# Captured: a file in a temp folder of the user profile.
SDDL_TEMP_FILE = f"O:{ME}D:(A;ID;FA;;;SY)(A;ID;FA;;;BA)(A;ID;FA;;;OW)"
# Captured: the same file after `icacls <file> /grant *S-1-5-32-545:(R)`.
SDDL_TEMP_FILE_USERS_READ = f"O:{ME}D:AI(A;;FR;;;BU)(A;ID;FA;;;SY)(A;ID;FA;;;BA)(A;ID;FA;;;OW)"
# Captured: C:\ProgramData
SDDL_PROGRAMDATA = ("O:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICIIO;GA;;;CO)(A;OICI;0x1200a9;;;BU)"
                    "(A;CI;DCLCRPCR;;;BU)")
# Captured: per-user Python install (python.exe)
SDDL_USER_PYTHON = f"O:{ME}D:AI(A;ID;FA;;;SY)(A;ID;FA;;;BA)(A;ID;FA;;;{ME})"

ON_WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows security APIs")


def test_service_sid_matches_sc_showsid() -> None:
    # `sc.exe showsid SentinelRecorder` on the development host printed exactly this SID.
    assert service_sid("SentinelRecorder") == "S-1-5-80-3978339998-2545952871-631246257-1716324596-627421672"
    assert service_sid("sentinelrecorder") == service_sid("SentinelRecorder")  # case-insensitive by design


def test_parse_sddl_abbreviations_and_rights() -> None:
    d = parse_sddl(SDDL_PROGRAMDATA)
    assert d.present and not d.null and d.protected and d.owner == SID_SYSTEM
    assert [a.sid for a in d.aces] == [SID_SYSTEM, SID_ADMINISTRATORS, "S-1-3-0", SID_USERS, SID_USERS]
    assert d.aces[2].inherit_only and not d.aces[0].inherit_only
    assert d.aces[3].mask == 0x1200A9
    assert d.aces[4].mask == 0x2 | 0x4 | 0x10 | 0x100


def test_owner_rights_ace_is_allowed_only_when_owner_is_allowed() -> None:
    d = parse_sddl(SDDL_TEMP_FILE)
    assert disallowed_grants(d, {SID_SYSTEM, SID_ADMINISTRATORS, ME}) == []
    probs = disallowed_grants(d, {SID_SYSTEM, SID_ADMINISTRATORS})
    assert len(probs) == 1 and "owner" in probs[0]


def test_users_read_grant_is_reported() -> None:
    probs = disallowed_grants(parse_sddl(SDDL_TEMP_FILE_USERS_READ), {SID_SYSTEM, SID_ADMINISTRATORS, ME})
    assert len(probs) == 1 and SID_USERS in probs[0] and "Users" in probs[0]


def test_inherit_only_and_deny_entries_do_not_count() -> None:
    d = parse_sddl("O:BAD:P(D;;FA;;;WD)(A;OICIIO;GA;;;BU)(A;;FA;;;SY)(A;;FA;;;BA)")
    assert disallowed_grants(d, {SID_SYSTEM, SID_ADMINISTRATORS}) == []


def test_null_and_missing_dacl_are_reported() -> None:
    assert "NULL" in disallowed_grants(parse_sddl("O:BAD:NO_ACCESS_CONTROL"), {SID_SYSTEM})[0]
    assert "no access control list" in disallowed_grants(parse_sddl("O:BA"), {SID_SYSTEM})[0]


def test_unknown_rights_code_is_treated_as_full_access() -> None:
    d = parse_sddl("D:(A;;QQ;;;BU)")
    assert d.aces[0].mask & winacl.GENERIC_ALL


def test_effective_allow_for_service_readability() -> None:
    py = parse_sddl(SDDL_USER_PYTHON)
    readers = [service_sid("SentinelRecorder"), winacl.SID_SERVICE, winacl.SID_AUTHENTICATED_USERS, SID_USERS]
    assert not any(effective_allow(py, s, winacl.FILE_READ_DATA) for s in readers)
    pd = parse_sddl(SDDL_PROGRAMDATA)
    assert effective_allow(pd, SID_USERS, winacl.FILE_READ_DATA)


@ON_WINDOWS
def test_real_file_dacl_and_secret_file_problems(tmp_path: Path) -> None:
    from sentinel.platform.windows import WindowsPlatform

    f = tmp_path / "camera.secret"
    f.write_text('username = "u"\npassword = "p"\n', encoding="utf-8")
    plat = WindowsPlatform()
    assert plat.secret_file_problems(f) == []          # profile temp: user, SYSTEM, Administrators only
    cp = subprocess.run(["icacls", str(f), "/grant", "*S-1-5-32-545:(R)"], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    probs = plat.secret_file_problems(f)
    assert len(probs) == 1 and SID_USERS in probs[0]


@ON_WINDOWS
def test_generated_icacls_commands_execute_and_produce_the_intended_acl(tmp_path: Path) -> None:
    """Runs the exact generated icacls argv on a temporary folder. The current user's SID stands
    in for the service SID (granting a not-yet-existing service account works the same way, but
    we must keep access to clean up). /setowner to Administrators needs elevation: NOT EXECUTED."""
    from sentinel.winsvc.commands import restrict_dir_steps, secret_file_acl_argv

    me = winacl.current_user_sid()
    d = tmp_path / "data"
    (d / "sub").mkdir(parents=True)
    child = d / "sub" / "seg.ts"
    child.write_bytes(b"x")
    subprocess.run(["icacls", str(child), "/grant", "*S-1-5-32-545:(R)"], check=True, capture_output=True)
    restrict, owner, reset = restrict_dir_steps(d, me, "M")
    for step in (restrict, reset):
        assert step.argv is not None
        cp = subprocess.run(list(step.argv), capture_output=True, text=True)
        assert cp.returncode == 0, cp.stdout + cp.stderr
    top = winacl.read_dacl(d)
    assert top.protected
    assert disallowed_grants(top, {SID_SYSTEM, SID_ADMINISTRATORS, me}) == []
    assert effective_allow(top, me, winacl.FILE_WRITE_DATA)
    sub = winacl.read_dacl(child)
    assert disallowed_grants(sub, {SID_SYSTEM, SID_ADMINISTRATORS, me}) == []   # extra Users grant removed
    assert all("ID" in a.flags for a in sub.aces)                                # inherited only

    secret = tmp_path / "camera.dpapi"
    secret.write_bytes(b"x")
    cp = subprocess.run(list(secret_file_acl_argv(secret, me)), capture_output=True, text=True)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    s = winacl.read_dacl(secret)
    assert s.protected and {a.sid for a in s.aces} == {SID_SYSTEM, SID_ADMINISTRATORS, me}
    mine = [a for a in s.aces if a.sid == me][0]
    assert mine.mask & winacl.FILE_READ_DATA and not mine.mask & winacl.FILE_WRITE_DATA
