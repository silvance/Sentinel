"""Windows access-control helpers (ctypes, no extra packages).

Merged at integration from two branches that each added this module (docs/INTEGRATION.md):

* windows-service: read a file's owner + DACL as SDDL (:func:`file_sddl`, :func:`read_dacl`),
  parse it (:func:`parse_sddl`) and judge who may use it (:func:`disallowed_grants`,
  :func:`effective_allow`); the per-service SID (:func:`service_sid`).
* single-executable: create Sentinel's own folders and secrets private **at creation**
  (:func:`private_sddl`, :func:`create_private_dir`, :func:`write_private_file`,
  :func:`protect_existing`, :func:`set_dacl`) and flag broad write access
  (:func:`broad_write_aces`, :func:`foreign_trustees`, :func:`get_sddl`).

Why SDDL and not ``icacls`` text: ``icacls`` prints localised account names
("VORDEFINIERT\\Administratoren" on German Windows) and localised status lines, so parsing its
output breaks on non-English installations. The security descriptor itself, rendered as SDDL by
``ConvertSecurityDescriptorToStringSecurityDescriptorW``, is language-neutral (SIDs and fixed
two-letter codes). ``icacls`` is still what we *generate* for administrators to change ACLs,
because it is the standard, inspectable tool (see ``sentinel.winsvc.commands``); it accepts
language-neutral ``*S-1-...`` SIDs.

The SDDL parsers and the rights evaluation are pure functions (unit-tested on any OS); the
functions below "Windows API" call Windows. Only the current user's own objects are changed;
nothing here changes system settings.
"""

from __future__ import annotations

import ctypes
import hashlib
import re
import struct
import sys
from collections.abc import Callable
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
            # Unknown right: be conservative and treat it as every right (both merged branches'
            # rule: single-executable expects 0xFFFFFFFF, windows-service "includes GENERIC_ALL").
            return 0xFFFFFFFF
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


# ---------------------------------------------------------------------- broad write access (single-executable)
# Well-known broad principals (SDDL aliases and the SIDs they stand for).
BROAD_TRUSTEES = {
    "WD": "Everyone", "S-1-1-0": "Everyone",
    "AU": "Authenticated Users", "S-1-5-11": "Authenticated Users",
    "BU": "Users", "S-1-5-32-545": "Users",
    "IU": "Interactive users", "S-1-5-4": "Interactive users",
    "BG": "Guests", "S-1-5-32-546": "Guests",
    "AN": "Anonymous", "S-1-5-7": "Anonymous",
    "S-1-5-32-547": "Power Users", "PU": "Power Users",
}
PRIVILEGED_TRUSTEES = {"SY", "S-1-5-18", "BA", "S-1-5-32-544", "OW", "S-1-3-4"}

# Access-mask bits that let a principal change a file or folder (or its contents/permissions).
WRITE_BITS = (0x0002 | 0x0004 | 0x0010 | 0x0040 | 0x0100 | 0x10000 | 0x40000 | 0x80000
              | 0x10000000 | 0x40000000 | 0x02000000)


def private_sddl(user_sid: str, inheritable: bool = True) -> str:
    flags = "OICI" if inheritable else ""
    return f"D:P(A;{flags};FA;;;{user_sid})(A;{flags};FA;;;SY)(A;{flags};FA;;;BA)"


def dacl_aces(sddl: str) -> list[tuple[str, str, int, str]]:
    """(type, flags, mask, trustee) for each ACE of the DACL part of an SDDL string."""
    dpart = sddl.split("D:", 1)[1] if "D:" in sddl else ""
    dpart = re.split(r"[SOG]:", dpart, maxsplit=1)[0]
    out = []
    for raw in _ACE_RE.findall(dpart):
        fields = raw.split(";")
        if len(fields) < 6:
            continue
        out.append((fields[0], fields[1], parse_rights(fields[2]), fields[5]))
    return out


def broad_write_aces(sddl: str) -> list[str]:
    """Broad principals that may modify the object itself (inherit-only ACEs ignored)."""
    if "D:NO_ACCESS_CONTROL" in sddl:
        return ["Everyone (no access control list)"]
    found = []
    for typ, flags, mask, trustee in dacl_aces(sddl):
        if typ != "A" or "IO" in flags:
            continue
        if trustee in BROAD_TRUSTEES and mask & WRITE_BITS:
            found.append(BROAD_TRUSTEES[trustee])
    return sorted(set(found))


def foreign_trustees(sddl: str, user_sid: str) -> list[str]:
    """Trustees other than ``user_sid``, SYSTEM and Administrators with any allow ACE."""
    allowed = PRIVILEGED_TRUSTEES | {user_sid}
    return sorted({t for typ, flags, _m, t in dacl_aces(sddl) if typ == "A" and t not in allowed})


# ---------------------------------------------------------------------- private creation (single-executable)
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
CREATE_NEW = 1
FILE_ATTRIBUTE_NORMAL = 0x80


def _api():  # noqa: ANN202
    if sys.platform != "win32":
        raise AclError("Windows access control is only available on Windows")
    import ctypes
    from ctypes import wintypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG)]
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    adv.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
    adv.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p]
    adv.SetNamedSecurityInfoW.restype = wintypes.DWORD
    adv.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL)]
    adv.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.OpenProcessToken.restype = wintypes.BOOL
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD)]
    adv.GetTokenInformation.restype = wintypes.BOOL
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertSidToStringSidW.restype = wintypes.BOOL
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.LocalFree.restype = ctypes.c_void_p
    k32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    k32.CreateDirectoryW.restype = wintypes.BOOL
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                              ctypes.c_void_p]
    k32.WriteFile.restype = wintypes.BOOL
    k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    k32.FlushFileBuffers.restype = wintypes.BOOL
    return ctypes, wintypes, adv, k32


def _fail(what: str) -> AclError:
    import ctypes
    err = ctypes.get_last_error()
    return AclError(err, f"{what} failed (Windows error {err})")


class _SecurityAttributes:
    """SECURITY_ATTRIBUTES built from SDDL; freed on exit."""

    def __init__(self, sddl: str) -> None:
        self.sddl = sddl

    def __enter__(self):  # noqa: ANN204
        ctypes, wintypes, adv, k32 = _api()

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", wintypes.BOOL)]

        self._k32 = k32
        self.psd = ctypes.c_void_p()
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(self.sddl, 1, ctypes.byref(self.psd), None):
            raise _fail("ConvertStringSecurityDescriptorToSecurityDescriptor")
        self.sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), self.psd, False)
        return self

    def __exit__(self, *_exc: object) -> None:
        self._k32.LocalFree(self.psd)


def get_sddl(path: Path) -> str:
    ctypes, wintypes, adv, k32 = _api()
    psd = ctypes.c_void_p()
    rc = adv.GetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None, None, None,
                                   ctypes.byref(psd))
    if rc != 0:
        raise AclError(rc, f"GetNamedSecurityInfo({path}) failed (Windows error {rc})")
    try:
        text = wintypes.LPWSTR()
        if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(psd, 1, DACL_SECURITY_INFORMATION,
                                                                        ctypes.byref(text), None):
            raise _fail("ConvertSecurityDescriptorToStringSecurityDescriptor")
        try:
            return str(text.value)
        finally:
            k32.LocalFree(text)
    finally:
        k32.LocalFree(psd)


def create_private_dir(path: Path, user_sid: str | None = None) -> None:
    """Create ``path`` (parents created normally) with the private, inheritable DACL."""
    _ctypes, _wt, _adv, k32 = _api()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _SecurityAttributes(private_sddl(user_sid or current_user_sid())) as sa:
        if not k32.CreateDirectoryW(str(path), _ctypes.byref(sa.sa)):
            raise _fail(f"CreateDirectory({path})")


def protect_existing(path: Path, user_sid: str | None = None) -> None:
    """Replace the DACL of an existing file/folder with the private one (children re-inherit)."""
    set_dacl(path, private_sddl(user_sid or current_user_sid(), inheritable=path.is_dir()))


def set_dacl(path: Path, sddl: str) -> None:
    """Set the DACL of ``path`` from an SDDL string (``D:P...`` = protected, not inherited)."""
    ctypes, wintypes, adv, k32 = _api()
    info = DACL_SECURITY_INFORMATION | (PROTECTED_DACL_SECURITY_INFORMATION if sddl.startswith("D:P") else 0)
    with _SecurityAttributes(sddl) as sa:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        pacl = ctypes.c_void_p()
        if not adv.GetSecurityDescriptorDacl(sa.psd, ctypes.byref(present), ctypes.byref(pacl),
                                             ctypes.byref(defaulted)):
            raise _fail("GetSecurityDescriptorDacl")
        rc = adv.SetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, info, None, None, pacl, None)
        if rc != 0:
            raise AclError(rc, f"SetNamedSecurityInfo({path}) failed (Windows error {rc})")


def strip_account_aces(sddl: str, drop: Callable[[str], bool]) -> tuple[str, list[str]]:
    """Pure: the DACL part of ``sddl`` without the explicit ACEs whose trustee ``drop`` selects,
    and the trustees removed. Inherited ACEs (ID) are left out of an unprotected result: Windows
    recomputes them from the parent. ACE text is kept verbatim."""
    idx = sddl.find("D:")
    if idx < 0:
        return sddl, []
    rest = sddl[idx + 2:]
    end = re.search(r"[SOG]:", rest)
    if end:
        rest = rest[:end.start()]
    head = rest.split("(", 1)[0]
    protected = "P" in head.replace("NO_ACCESS_CONTROL", "")
    kept: list[str] = []
    removed: list[str] = []
    for body in _ACE_RE.findall(rest):
        parts = body.split(";")
        flags = parts[1].upper() if len(parts) > 1 else ""
        sid = sid_from_sddl(parts[5]) if len(parts) >= 6 else ""
        inherited = "ID" in _split2(flags)
        if not inherited and sid and drop(sid):
            removed.append(sid)
            continue
        if inherited and not protected:
            continue
        kept.append(f"({body})")
    return "D:" + ("P" if protected else "") + "".join(kept), sorted(set(removed))


def sid_is_user_account(sid: str) -> bool:
    """True for a local or domain *user* account (S-1-5-21-…, SidTypeUser), or such a SID that no
    longer resolves (a deleted account). Groups, service accounts and well-known SIDs are False."""
    sid = sid.upper()
    if not sid.startswith("S-1-5-21-"):
        return False
    ctypes, wintypes, adv, k32 = _api()
    adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertStringSidToSidW.restype = wintypes.BOOL
    adv.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
                                      ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_int)]
    adv.LookupAccountSidW.restype = wintypes.BOOL
    psid = ctypes.c_void_p()
    if not adv.ConvertStringSidToSidW(sid, ctypes.byref(psid)):
        raise _fail(f"ConvertStringSidToSid({sid})")
    try:
        name, dom = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        n, d, use = wintypes.DWORD(256), wintypes.DWORD(256), ctypes.c_int()
        if not adv.LookupAccountSidW(None, psid, name, ctypes.byref(n), dom, ctypes.byref(d), ctypes.byref(use)):
            return ctypes.get_last_error() == 1332          # ERROR_NONE_MAPPED: a deleted account
        return use.value == 1                               # SidTypeUser
    finally:
        k32.LocalFree(psid)


def sid_is_account(sid: str) -> bool:
    """Any local or domain user or group account (S-1-5-21-…)."""
    return sid.upper().startswith("S-1-5-21-")


def remove_account_aces(path: Path, drop: Callable[[str], bool] = sid_is_user_account) -> list[str]:
    """Remove the explicit ACEs of the accounts ``drop`` selects (default: user accounts) from
    ``path``; returns the SIDs removed. Review 2026-11-12 (C-001 / install refusal): setup and the
    credential writers give the account that ran them Full Control, and icacls ``/grant:r`` keeps
    it, so the installed service refused its own credentials file and configuration folder."""
    new, removed = strip_account_aces(get_sddl(path), drop)
    if removed:
        set_dacl(path, new)
    return removed


def write_private_file(path: Path, data: bytes, user_sid: str | None = None,
                       extra_read_sids: tuple[str, ...] = ()) -> None:
    """Create a NEW file readable only by this account/SYSTEM/Administrators and write ``data``.
    ``extra_read_sids`` (e.g. the recorder service) also get read-only access, from creation.
    The handle is not shared (share mode 0), so nobody else can open the file while it is written.

    Fails if the file exists (CREATE_NEW): callers write a temporary name and rename it."""
    ctypes, wintypes, adv, k32 = _api()
    sddl = private_sddl(user_sid or current_user_sid(), inheritable=False)
    for s in extra_read_sids:
        if not re.fullmatch(r"S-1-[0-9-]+", s):
            raise AclError(f"not a SID: {s!r}")
        sddl += f"(A;;FR;;;{s})"
    with _SecurityAttributes(sddl) as sa:
        handle = k32.CreateFileW(str(path), GENERIC_WRITE, 0, ctypes.byref(sa.sa), CREATE_NEW,
                                 FILE_ATTRIBUTE_NORMAL, None)
        if handle in (None, wintypes.HANDLE(-1).value):
            raise _fail(f"CreateFile({path})")
        try:
            written = wintypes.DWORD()
            buf = ctypes.create_string_buffer(data, len(data))
            if not k32.WriteFile(handle, buf, len(data), ctypes.byref(written), None) or written.value != len(data):
                raise _fail("WriteFile")
            if not k32.FlushFileBuffers(handle):
                raise _fail("FlushFileBuffers")
        finally:
            k32.CloseHandle(handle)
