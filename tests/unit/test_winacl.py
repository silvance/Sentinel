"""SDDL evaluation used to refuse locations other accounts could change (pure; runs everywhere)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sentinel.platform import winacl

# DACL of C:\ProgramData on the development host (read with GetNamedSecurityInfoW).
PROGRAMDATA = "D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICIIO;GA;;;CO)(A;OICI;0x1200a9;;;BU)(A;CI;DCLCRPCR;;;BU)"
ME = "S-1-5-21-1-2-3-1001"


def test_rights_parsing() -> None:
    assert winacl.parse_rights("FA") == 0x1F01FF
    assert winacl.parse_rights("0x1200a9") == 0x1200A9
    assert winacl.parse_rights("DCLCRPCR") == 0x2 | 0x4 | 0x10 | 0x100
    assert winacl.parse_rights("ZZ") == 0xFFFFFFFF          # unknown => assume everything


def test_programdata_is_writable_by_users_but_read_only_ace_is_not_counted() -> None:
    assert winacl.broad_write_aces(PROGRAMDATA) == ["Users"]
    assert winacl.broad_write_aces("D:(A;OICI;0x1200a9;;;BU)") == []          # read & execute only
    assert winacl.broad_write_aces("D:(A;OICIIO;FA;;;WD)") == []              # inherit-only: not this object
    assert winacl.broad_write_aces("D:(A;;FW;;;AU)(A;;GA;;;S-1-1-0)") == ["Authenticated Users", "Everyone"]
    assert winacl.broad_write_aces("D:NO_ACCESS_CONTROL")


def test_private_sddl_has_no_foreign_trustees() -> None:
    sddl = winacl.private_sddl(ME)
    assert sddl.startswith("D:P") and winacl.foreign_trustees(sddl, ME) == []
    assert winacl.broad_write_aces(sddl) == []
    assert winacl.foreign_trustees(PROGRAMDATA, ME) == ["BU", "CO"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_real_programdata_is_flagged() -> None:
    from sentinel.platform import get_platform
    assert "can be changed by Users" in get_platform().write_access_problems(Path("C:/ProgramData"))
