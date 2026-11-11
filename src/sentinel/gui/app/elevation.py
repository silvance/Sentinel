"""Administrator rights for the window.

Settings live in a protected folder and the recorder runs as a Windows service, so changing them
needs administrator rights. The window never stores or asks for a password itself: "Unlock" asks
Windows (UAC) to start the window again as administrator, and Windows decides.
"""

from __future__ import annotations

import os
import subprocess
import sys

from ...paths import is_frozen


def is_admin() -> bool:
    if sys.platform == "win32":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (OSError, AttributeError):
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def elevated_argv(extra: list[str]) -> tuple[str, list[str]]:
    """(program, arguments) that reopen this window: ``Sentinel.exe launch ...``."""
    if is_frozen():
        return sys.executable, ["launch", *extra]
    return sys.executable, ["-m", "sentinel.launcher", "launch", *extra]


def relaunch_elevated(extra: list[str]) -> bool:
    """Ask Windows to start the window as administrator. True if Windows started it (the person
    approved the prompt); False if they declined or this is not Windows."""
    if sys.platform != "win32":
        return False
    import ctypes

    program, args = elevated_argv(extra)
    params = subprocess.list2cmdline(args)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", program, params, None, 1)   # SW_SHOWNORMAL
    return int(rc) > 32
