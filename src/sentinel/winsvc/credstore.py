"""Write the DPAPI-protected camera credentials file (``set-camera-credentials``).

Order of operations, so the secret is never on disk with a wide ACL and never half-written:
1. create an empty temporary file next to the target;
2. restrict its ACL (SYSTEM, Administrators, service account) with ``icacls``;
3. write the protected blob into it and flush it to disk;
4. read it back through :func:`sentinel.credentials.load_credentials` and compare;
5. atomically replace the target (a rename keeps the restricted ACL).
The plaintext exists only in this process's memory; it is never logged or printed.
"""

from __future__ import annotations

import base64
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from ..credentials import DPAPI_MAGIC, CredentialError, credentials_toml, load_credentials
from . import dpapi

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def protected_file_bytes(username: str, password: str, scope: dpapi.Scope = "machine") -> bytes:
    if not username:
        raise CredentialError("the camera user name must not be empty")
    blob = dpapi.protect(credentials_toml(username, password).encode("utf-8"), scope=scope)
    b64 = base64.b64encode(blob)
    lines = [b64[i:i + 76] for i in range(0, len(b64), 76)]
    return DPAPI_MAGIC + f" scope={scope}".encode("ascii") + b"\n" + b"\n".join(lines) + b"\n"


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=60, check=False)


def write_protected_credentials(path: Path, username: str, password: str, *, scope: dpapi.Scope = "machine",
                                acl_argv: Callable[[Path], Sequence[str]] | None = None,
                                run: Runner = _default_runner) -> None:
    """``acl_argv(tmp_path)`` returns the icacls command restricting the file (None = keep the
    inherited ACL, for development only)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.touch()
    try:
        if acl_argv is not None:
            argv = list(acl_argv(tmp))
            cp = run(argv)
            if cp.returncode != 0:
                raise CredentialError(f"could not restrict the permissions of the credentials file "
                                      f"(icacls exit {cp.returncode}: {(cp.stdout or '').strip()} "
                                      f"{(cp.stderr or '').strip()})")
        data = protected_file_bytes(username, password, scope)
        with open(tmp, "r+b") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        back = load_credentials(tmp)
        if back.username != username or back.password != password or back.protection != "dpapi":
            raise CredentialError("verification failed: the protected file did not decrypt to the same values")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
