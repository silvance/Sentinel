"""Windows DPAPI (CryptProtectData / CryptUnprotectData) through ctypes — no extra dependency.

What this protects against, honestly stated:
* The camera password is not stored as readable text on disk. Copying the protected file to
  another computer does not reveal it (the key is bound to this Windows installation).
* **Machine scope** (``CRYPTPROTECT_LOCAL_MACHINE``) is used so the service's virtual account can
  decrypt a blob written by an administrator. The consequence: *any* process on this machine
  that can read the file can decrypt it. The file's ACL (SYSTEM, Administrators and the service
  account only — applied by ``set-camera-credentials`` and checked at service start) is
  therefore what keeps other local users out. DPAPI does not protect against an administrator or
  against offline extraction of the machine's DPAPI keys from an unencrypted disk; full-disk
  encryption (BitLocker) is the control for a stolen disk.
* The additional entropy is a fixed, application-specific value. It is not a secret (it is in
  this source code); it only prevents another program that blindly calls CryptUnprotectData on
  the file from succeeding without knowing Sentinel's value.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Literal

CRYPTPROTECT_UI_FORBIDDEN = 0x1
CRYPTPROTECT_LOCAL_MACHINE = 0x4

ENTROPY = b"Sentinel camera credentials / DPAPI v1"
DESCRIPTION = "Sentinel camera credentials"

Scope = Literal["machine", "user"]


class DpapiError(OSError):
    """DPAPI call failed; the message never contains the protected data."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:  # keep the buffer alive with the struct
    buf = (ctypes.c_ubyte * max(len(data), 1)).from_buffer_copy(data or b"\0")
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf


def _crypt32():  # noqa: ANN202
    if sys.platform != "win32":
        raise DpapiError("DPAPI is only available on Windows")
    from ctypes import wintypes

    c32 = ctypes.WinDLL("crypt32", use_last_error=True)
    sig = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob), ctypes.c_void_p,
           ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    c32.CryptProtectData.argtypes = sig
    c32.CryptProtectData.restype = wintypes.BOOL
    c32.CryptUnprotectData.argtypes = [ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR),
                                       ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    c32.CryptUnprotectData.restype = wintypes.BOOL
    return c32


def _take(out: _DataBlob) -> bytes:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.LocalFree.restype = ctypes.c_void_p
    try:
        data = ctypes.string_at(out.pbData, out.cbData)
        ctypes.memset(out.pbData, 0, out.cbData)   # scrub the unmanaged copy before freeing it
        return data
    finally:
        k32.LocalFree(out.pbData)


def protect(plaintext: bytes, scope: Scope = "machine", entropy: bytes = ENTROPY) -> bytes:
    c32 = _crypt32()
    flags = CRYPTPROTECT_UI_FORBIDDEN | (CRYPTPROTECT_LOCAL_MACHINE if scope == "machine" else 0)
    data_in, _b1 = _blob(plaintext)
    ent, _b2 = _blob(entropy)
    out = _DataBlob()
    if not c32.CryptProtectData(ctypes.byref(data_in), DESCRIPTION, ctypes.byref(ent), None, None, flags,
                                ctypes.byref(out)):
        err = ctypes.get_last_error()
        raise DpapiError(f"CryptProtectData failed (Windows error {err})")
    ctypes.memset(_b1, 0, len(_b1))
    return _take(out)


def unprotect(blob: bytes, entropy: bytes = ENTROPY) -> bytes:
    c32 = _crypt32()
    data_in, _b1 = _blob(blob)
    ent, _b2 = _blob(entropy)
    out = _DataBlob()
    if not c32.CryptUnprotectData(ctypes.byref(data_in), None, ctypes.byref(ent), None, None,
                                  CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        err = ctypes.get_last_error()
        raise DpapiError(f"CryptUnprotectData failed (Windows error {err}); the file was protected on "
                         "another computer, by another account, or is damaged")
    return _take(out)
