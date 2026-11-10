"""Windows access-control helpers: read a file's DACL as SDDL and judge who can use it.

Why SDDL and not ``icacls`` text: ``icacls`` prints localised account names
("VORDEFINIERT\\Administratoren" on German Windows) and localised status lines, so parsing its
output breaks on non-English installations. The security descriptor itself, rendered as SDDL by
``ConvertSecurityDescriptorToStringSecurityDescriptorW``, is language-neutral (SIDs and fixed
two-letter codes). ``icacls`` is still what we *generate* for administrators to change ACLs,
because it is the standard, inspectable tool (see ``sentinel.winsvc.commands``); it accepts
language-neutral ``*S-1-...`` SIDs.

The SDDL parser and the rights evaluation are pure functions (unit-tested on any OS); only
:func:`file_sddl` and :func:`current_user_sid` call Windows APIs.
"""

from __future__ import annotations

import ctypes
import hashlib
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

# Well-known SIDs (language-neutral). SDDL abbreviations map to these.
SID_SYSTEM = "S-1-5-18"
SID_ADMINISTRATORS = "S-1-5-32-544"
SID_USERS = "S-1-5-32-545"
SID_EVERYONE = "S-1-1-0"
SID_AUTHENTICATED_USERS = "S-1-5-11"
SID_SERVICE = "S-1-5-6"            # every service logon carries this group
SID_OWNER_RIGHTS = "S-1-3-4"
SID_CREATOR_OWNER = "S-1-3-0"

_SDDL_SID_ABBREV = {
    "SY": SID_SYSTEM, "BA": SID_ADMINISTRATORS, "BU": SID_USERS, "WD": SID_EVERYONE,
    "AU": SID_AUTHENTICATED_USERS, "SU": SID_SERVICE, "OW": SID_OWNER_RIGHTS, "CO": SID_CREATOR_OWNER,
    "CG": "S-1-3-1", "IU": "S-1-5-4", "NU": "S-1-5-2", "AN": "S-1-5-7", "LS": "S-1-5-19",
    "NS": "S-1-5-20", "BG": "S-1-5-32-546", "PU": "S-1-5-32-547", "BO": "S-1-5-32-551",
    "SO": "S-1-5-32-549", "AO": "S-1-5-32-548", "RD": "S-1-5-32-555", "RU": "S-1-5-32-554",
    "AC": "S-1-15-2-1", "RC": "S-1-5-12", "WR": "S-1-5-33", "PS": "S-1-5-10", "ED": "S-1-5-9",
    "LU": "S-1-5-32-559", "MU": "S-1-5-32-558", "NO": "S-1-5-32-556", "HI": "S-1-16-12288",
    "ME": "S-1-16-8192", "LW": "S-1-16-4096", "SI": "S-1-16-16384", "IS": "S-1-5-32-568",
}

# Access-mask bits (winnt.h) and SDDL rights codes.
FILE_READ_DATA = 0x0001
FILE_WRITE_DATA = 0x0002
FILE_APPEND_DATA = 0x0004
FILE_EXECUTE = 0x0020
DELETE = 0x00010000
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000
GENERIC_ALL = 0x10000000
GENERIC_EXECUTE = 0x20000000
GENERIC_WRITE = 0x40000000
GENERIC_READ = 0x80000000
FILE_ALL_ACCESS = 0x001F01FF

_RIGHTS = {
    "GA": GENERIC_ALL, "GR": GENERIC_READ, "GW": GENERIC_WRITE, "GX": GENERIC_EXECUTE,
    "RC": READ_CONTROL, "SD": DELETE, "WD": WRITE_DAC, "WO": WRITE_OWNER,
    "RP": 0x10, "WP": 0x20, "CC": 0x1, "DC": 0x2, "LC": 0x4, "SW": 0x8, "LO": 0x80, "DT": 0x40, "CR": 0x100,
    "FA": FILE_ALL_ACCESS, "FR": 0x00120089, "FW": 0x00120116, "FX": 0x001200A0,
    "KA": 0x000F003F, "KR": 0x00020019, "KW": 0x00020006, "KX": 0x00020019,
}

# Rights that let someone read the secret, change it, or change who may do so.
READ_RIGHTS = FILE_READ_DATA | GENERIC_READ | GENERIC_ALL
SIGNIFICANT_RIGHTS = (FILE_READ_DATA | FILE_WRITE_DATA | FILE_APPEND_DATA | DELETE | WRITE_DAC
                      | WRITE_OWNER | GENERIC_ALL | GENERIC_WRITE | GENERIC_READ)
MODIFY_RIGHTS = FILE_READ_DATA | FILE_WRITE_DATA | FILE_APPEND_DATA | DELETE


class AclError(OSError):
    """The security descriptor could not be read or parsed."""


@dataclass(frozen=True)
class Ace:
    ace_type: str        # "A" allow, "D" deny, "OA"/"OD" object, "XA"/"XD" callback
    flags: str           # e.g. "OICIID"
    mask: int
    sid: str             # full S-1-... string

    @property
    def allows(self) -> bool:
        return self.ace_type in ("A", "OA", "XA", "ZA")

    @property
    def inherit_only(self) -> bool:
        return "IO" in _split2(self.flags)


@dataclass(frozen=True)
class Dacl:
    present: bool        # False => no DACL => everyone has full access
    null: bool           # "D:NO_ACCESS_CONTROL"
    protected: bool      # "P" flag: inheritance from the parent is blocked
    aces: tuple[Ace, ...]
    owner: str | None = None


def _split2(s: str) -> list[str]:
    return [s[i:i + 2] for i in range(0, len(s), 2)]


def sid_from_sddl(token: str) -> str:
    token = token.strip()
    if token.upper().startswith("S-1-"):
        return token.upper()
    return _SDDL_SID_ABBREV.get(token.upper(), token.upper())


def parse_rights(text: str) -> int:
    text = text.strip()
    if not text:
        return 0
    if text.lower().startswith("0x"):
        return int(text, 16)
    if text.isdigit():
        return int(text)
    mask = 0
    for code in _split2(text.upper()):
        if code not in _RIGHTS:
            # Unknown right: be conservative and treat it as full access.
            return FILE_ALL_ACCESS | GENERIC_ALL
        mask |= _RIGHTS[code]
    return mask


_ACE_RE = re.compile(r"\(([^()]*)\)")


def parse_sddl(sddl: str) -> Dacl:
    """Parse the owner and DACL parts of an SDDL string (SACL/label parts are ignored)."""
    owner = None
    m = re.search(r"O:(S-1-[0-9-]+|[A-Z]{2})", sddl)
    if m:
        owner = sid_from_sddl(m.group(1))
    idx = sddl.find("D:")
    if idx < 0:
        return Dacl(present=False, null=False, protected=False, aces=(), owner=owner)
    rest = sddl[idx + 2:]
    end = rest.find("S:")
    if end >= 0:
        rest = rest[:end]
    head = rest.split("(", 1)[0]
    if "NO_ACCESS_CONTROL" in head:
        return Dacl(present=True, null=True, protected="P" in head.replace("NO_ACCESS_CONTROL", ""),
                    aces=(), owner=owner)
    aces = []
    for body in _ACE_RE.findall(rest):
        parts = body.split(";")
        if len(parts) < 6:
            raise AclError(f"malformed ACE in SDDL: ({body})")
        ace_type, flags, rights, sid = parts[0].upper(), parts[1].upper(), parts[2], parts[5]
        aces.append(Ace(ace_type, flags, parse_rights(rights), sid_from_sddl(sid)))
    return Dacl(present=True, null=False, protected="P" in head, aces=tuple(aces), owner=owner)


def effective_allow(dacl: Dacl, sid: str, mask: int) -> bool:
    """True if some non-inherit-only allow ACE grants ``sid`` any bit of ``mask``.

    This deliberately ignores deny ACEs and group membership expansion beyond exact SID
    matches; callers pass every SID they care about. It answers "is access *granted* to this
    principal by an entry naming it", which is the right question for a too-permissive check.
    """
    if not dacl.present or dacl.null:
        return True
    return any(a.allows and not a.inherit_only and a.sid == sid and a.mask & mask for a in dacl.aces)


def disallowed_grants(dacl: Dacl, allowed_sids: set[str], mask: int = SIGNIFICANT_RIGHTS) -> list[str]:
    """Return human-readable problems for principals outside ``allowed_sids`` that are granted
    any bit of ``mask``. An empty list means only allowed principals have such rights."""
    if not dacl.present:
        return ["has no access control list (anyone can read and change it)"]
    if dacl.null:
        return ["has an empty (NULL) access control list (anyone can read and change it)"]
    allowed = {s.upper() for s in allowed_sids}
    problems = []
    for a in dacl.aces:
        if not a.allows or a.inherit_only or not a.mask & mask:
            continue
        sid = a.sid
        if sid == SID_OWNER_RIGHTS:
            if dacl.owner in allowed:
                continue
            sid = f"owner ({dacl.owner})"
        elif sid in allowed:
            continue
        problems.append(f"grants access mask 0x{a.mask:08x} to {sid} ({_describe(a.sid)})")
    return problems


_NAMES = {SID_EVERYONE: "Everyone", SID_USERS: "Users", SID_AUTHENTICATED_USERS: "Authenticated Users",
          SID_SYSTEM: "SYSTEM", SID_ADMINISTRATORS: "Administrators", SID_SERVICE: "SERVICE",
          SID_OWNER_RIGHTS: "OWNER RIGHTS", "S-1-5-4": "INTERACTIVE", "S-1-5-19": "LOCAL SERVICE",
          "S-1-5-20": "NETWORK SERVICE", "S-1-5-32-546": "Guests"}


def _describe(sid: str) -> str:
    if sid in _NAMES:
        return _NAMES[sid]
    if sid.startswith("S-1-5-80-"):
        return "a service account"
    if sid.startswith("S-1-5-21-"):
        return "a user or group account"
    return "other principal"


def service_sid(service_name: str) -> str:
    """The per-service SID (``NT SERVICE\\<name>``): S-1-5-80 + SHA-1 of the upper-cased name.

    Deterministic and computable before the service exists, which is what lets the installer
    generate ``icacls`` grants for the virtual account. Verified against ``sc.exe showsid``.
    """
    digest = hashlib.sha1(service_name.upper().encode("utf-16-le")).digest()
    subs = struct.unpack("<5I", digest)
    return "S-1-5-80-" + "-".join(str(s) for s in subs)


# ---------------------------------------------------------------------- Windows API (ctypes)
SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x1
DACL_SECURITY_INFORMATION = 0x4
SDDL_REVISION_1 = 1
TOKEN_QUERY = 0x0008
TokenUser = 1


def _advapi():  # noqa: ANN202
    if sys.platform != "win32":
        raise AclError("Windows security APIs are only available on Windows")
    from ctypes import wintypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                          ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(ctypes.c_void_p)]
    adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG)]
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertSidToStringSidW.restype = wintypes.BOOL
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.OpenProcessToken.restype = wintypes.BOOL
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD)]
    adv.GetTokenInformation.restype = wintypes.BOOL
    return adv


def _kernel():  # noqa: ANN202
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.LocalFree.restype = ctypes.c_void_p
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def file_sddl(path: Path) -> str:
    """Owner + DACL of ``path`` as an SDDL string (needs only READ_CONTROL on the file)."""
    from ctypes import wintypes

    adv, k32 = _advapi(), _kernel()
    sd = ctypes.c_void_p()
    rc = adv.GetNamedSecurityInfoW(str(path), SE_FILE_OBJECT,
                                   OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                                   None, None, None, None, ctypes.byref(sd))
    if rc != 0:
        raise AclError(rc, f"cannot read the security descriptor of {path} (Windows error {rc})")
    try:
        out = wintypes.LPWSTR()
        if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                sd, SDDL_REVISION_1, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                ctypes.byref(out), None):
            err = ctypes.get_last_error()
            raise AclError(err, f"cannot convert the security descriptor of {path} (Windows error {err})")
        try:
            return str(out.value)
        finally:
            k32.LocalFree(out)
    finally:
        k32.LocalFree(sd)


def current_user_sid() -> str:
    """SID of the account this process runs as (e.g. the virtual service account)."""
    from ctypes import wintypes

    adv, k32 = _advapi(), _kernel()
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise AclError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        size = wintypes.DWORD()
        adv.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, TokenUser, buf, size, ctypes.byref(size)):
            raise AclError(ctypes.get_last_error(), "GetTokenInformation failed")
        psid = ctypes.c_void_p.from_buffer(buf).value   # TOKEN_USER.User.Sid is the first field
        out = wintypes.LPWSTR()
        if not adv.ConvertSidToStringSidW(psid, ctypes.byref(out)):
            raise AclError(ctypes.get_last_error(), "ConvertSidToStringSid failed")
        try:
            return str(out.value).upper()
        finally:
            k32.LocalFree(out)
    finally:
        k32.CloseHandle(token)


def read_dacl(path: Path) -> Dacl:
    return parse_sddl(file_sddl(path))
