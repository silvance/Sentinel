"""Running Windows PowerShell scripts without writing them to disk.

Scripts are passed with ``-EncodedCommand`` (base64 of UTF-16LE), so there is no script file that
another local user could alter between creation and (elevated) execution, and no quoting of
hardware-supplied strings on a command line. Execution policy does not apply to
``-EncodedCommand`` (it governs script files).

Windows PowerShell 5.1 (``powershell.exe``) is used, not PowerShell 7, because 5.1 ships with
every supported Windows release; the Storage module cmdlets used here exist in both
(verified on the development host: Windows 11 26200, PowerShell 5.1.26100 and 7.6.0).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .errors import DriveQueryError

CREATE_NO_WINDOW = 0x08000000
# Windows command lines are limited to 32,767 characters; keep encoded scripts well below.
MAX_ENCODED_CHARS = 24_000


def powershell_exe() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    exe = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if exe.is_file():
        return str(exe)
    found = shutil.which("powershell.exe")
    if found:
        return found
    raise DriveQueryError("powershell_missing", "Windows PowerShell (powershell.exe) was not found")


def encode_command(script: str) -> str:
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    if len(enc) > MAX_ENCODED_CHARS:
        raise ValueError(f"encoded PowerShell script too long ({len(enc)} chars)")
    return enc


def ps_b64_json(obj: Any) -> str:
    """Embed data in a script as base64 JSON (decoded inside the script; no quoting problems)."""
    return base64.b64encode(json.dumps(obj, sort_keys=True).encode("utf-8")).decode("ascii")


def base_args() -> list[str]:
    return [powershell_exe(), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand"]


def run_powershell(script: str, timeout_s: float = 120.0) -> str:
    """Run a script (unelevated) and return stdout. Raises DriveQueryError on failure."""
    argv = base_args() + [encode_command(script)]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout_s, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise DriveQueryError("powershell_timeout", f"PowerShell did not finish within {timeout_s:.0f} s") from None
    except OSError as exc:
        raise DriveQueryError("powershell_failed", f"PowerShell could not be started: {exc}") from None
    out = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise DriveQueryError("powershell_failed", f"PowerShell exited with {proc.returncode}",
                              {"stderr": err[-2000:]})
    return out
