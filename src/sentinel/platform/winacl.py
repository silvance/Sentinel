"""Windows access control for Sentinel's own files and folders (ctypes, no extra packages).

* :func:`private_sddl` — DACL used for everything setup creates: the current account, SYSTEM
  and Administrators only, *protected* (nothing inherited from ``%ProgramData%``, whose default
  ACL lets every local user read files and create files in subfolders).
* :func:`create_private_dir` / :func:`write_private_file` apply that DACL **at creation**
  (``CreateDirectoryW`` / ``CreateFileW`` with ``SECURITY_ATTRIBUTES``), so there is never a
  moment in which a secret or a folder carries the inherited, readable ACL.
* :func:`protect_existing` replaces the DACL of an existing folder (propagated to children).
* :func:`broad_write_aces` / :func:`foreign_trustees` evaluate an SDDL string; they are pure
  functions and unit-tested on every platform.

Only the current user's own objects are touched; nothing here changes system settings.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

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
_RIGHT_CODES = {
    "GA": 0x10000000, "GR": 0x80000000, "GW": 0x40000000, "GX": 0x20000000,
    "FA": 0x001F01FF, "FR": 0x00120089, "FW": 0x00120116, "FX": 0x001200A0,
    "SD": 0x10000, "RC": 0x20000, "WD": 0x40000, "WO": 0x80000,
    "CC": 0x1, "DC": 0x2, "LC": 0x4, "SW": 0x8, "RP": 0x10, "WP": 0x20, "DT": 0x40, "LO": 0x80, "CR": 0x100,
    "KA": 0x000F003F, "KR": 0x00020019, "KW": 0x00020006, "KX": 0x00020019,
}
_ACE_RE = re.compile(r"\(([^)]*)\)")


def private_sddl(user_sid: str, inheritable: bool = True) -> str:
    flags = "OICI" if inheritable else ""
    return f"D:P(A;{flags};FA;;;{user_sid})(A;{flags};FA;;;SY)(A;{flags};FA;;;BA)"


def parse_rights(text: str) -> int:
    text = text.strip()
    if text.lower().startswith("0x"):
        return int(text, 16)
    mask = 0
    for i in range(0, len(text), 2):
        code = text[i:i + 2]
        if code not in _RIGHT_CODES:
            return 0xFFFFFFFF  # unknown right: treat as everything (conservative)
        mask |= _RIGHT_CODES[code]
    return mask


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


# ---------------------------------------------------------------------- Win32 (Windows only)
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x4
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
GENERIC_WRITE = 0x40000000
CREATE_NEW = 1
FILE_ATTRIBUTE_NORMAL = 0x80
TOKEN_QUERY = 0x8
TokenUser = 1


class AclError(OSError):
    pass


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


def current_user_sid() -> str:
    ctypes, wintypes, adv, k32 = _api()
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise _fail("OpenProcessToken")
    try:
        size = wintypes.DWORD(0)
        adv.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, TokenUser, buf, size, ctypes.byref(size)):
            raise _fail("GetTokenInformation")
        psid = ctypes.c_void_p.from_buffer(buf).value   # TOKEN_USER.User.Sid is the first field
        text = wintypes.LPWSTR()
        if not adv.ConvertSidToStringSidW(psid, ctypes.byref(text)):
            raise _fail("ConvertSidToStringSid")
        try:
            return str(text.value)
        finally:
            k32.LocalFree(text)
    finally:
        k32.CloseHandle(token)


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


def write_private_file(path: Path, data: bytes, user_sid: str | None = None) -> None:
    """Create a NEW file readable only by this account/SYSTEM/Administrators and write ``data``.

    Fails if the file exists (CREATE_NEW): callers write a temporary name and rename it."""
    ctypes, wintypes, adv, k32 = _api()
    with _SecurityAttributes(private_sddl(user_sid or current_user_sid(), inheritable=False)) as sa:
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
