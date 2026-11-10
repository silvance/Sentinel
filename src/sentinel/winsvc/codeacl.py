"""Refuse to run the service from code that non-administrators can change.

The service runs as its own account; if a standard user can write the interpreter, a file on
PYTHONPATH, the Sentinel package, FFmpeg, or the configuration (which names the FFmpeg path), that
user can run code as the service and alter or suppress recordings. Classic case: a folder created
directly under ``C:\\`` inherits *Authenticated Users: Modify*.

Rule: for every code path, only SYSTEM, Administrators and TrustedInstaller (and OWNER RIGHTS when
the owner is one of them) may hold write-type rights, and the owner must be one of them (an owner
can always rewrite the ACL). Write-type = write/append data (= add file / add subfolder on a
folder), delete, delete child, write DAC, write owner, generic write/all. Deny entries are not
credited (a deny for one group does not stop a user reached through another).

``scan`` walks directory trees (every folder and file — a single writable ``.py`` or ``.pth`` file
is enough; ``__pycache__`` is included because a planted ``.pyc`` is code too). Results are
cached by SDDL so large trees with inherited ACLs are cheap.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path

from ..platform import winacl

SID_TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
TRUSTED_WRITERS = frozenset({winacl.SID_SYSTEM, winacl.SID_ADMINISTRATORS, SID_TRUSTED_INSTALLER})
FILE_DELETE_CHILD = 0x40
WRITE_RIGHTS = (winacl.FILE_WRITE_DATA | winacl.FILE_APPEND_DATA | winacl.DELETE | FILE_DELETE_CHILD
                | winacl.WRITE_DAC | winacl.WRITE_OWNER | winacl.GENERIC_WRITE | winacl.GENERIC_ALL)


def write_problems(dacl: winacl.Dacl) -> list[str]:
    """Who other than trusted writers can change an object with this DACL/owner (pure)."""
    probs = winacl.disallowed_grants(dacl, set(TRUSTED_WRITERS), WRITE_RIGHTS)
    if dacl.owner is not None and dacl.owner not in TRUSTED_WRITERS:
        probs.append(f"is owned by {dacl.owner}, who can change its permissions")
    return probs


def scan(paths: Iterable[Path], recursive_roots: Iterable[Path] = (),
         read_sddl: Callable[[Path], str] = winacl.file_sddl, limit: int = 20) -> list[str]:
    """Problems for ``paths`` (each checked itself) and every file and folder under
    ``recursive_roots``. Stops after ``limit`` problems (enough to refuse, short to read)."""
    cache: dict[str, list[str]] = {}
    out: list[str] = []

    def check(p: Path) -> None:
        try:
            sddl = read_sddl(p)
        except (winacl.AclError, OSError) as exc:
            out.append(f"{p}: permissions could not be read ({exc})")
            return
        if sddl not in cache:
            cache[sddl] = write_problems(winacl.parse_sddl(sddl))
        out.extend(f"{p} {msg}" for msg in cache[sddl])

    for p in paths:
        if len(out) >= limit:
            break
        check(Path(p))
    for root in recursive_roots:
        for dirpath, dirnames, filenames in os.walk(root):
            if len(out) >= limit:
                return out[:limit]
            check(Path(dirpath))
            for name in filenames:
                check(Path(dirpath) / name)
    return out[:limit]
