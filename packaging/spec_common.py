"""Settings shared by packaging/windows/sentinel.spec and packaging/linux/sentinel.spec.

Imported by the spec files (which PyInstaller executes); kept free of PyInstaller imports so
it can also be read by the build script.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ENTRY = ROOT / "packaging" / "entry_sentinel.py"
# Windows only (integration): the service host as a second executable in the same one-folder
# build, sharing _internal\ (docs/INTEGRATION.md, sentinel.winsvc.commands.SERVICE_EXE_NAME).
SERVICE_ENTRY = ROOT / "packaging" / "entry_service.py"
SERVICE_EXE_NAME = "SentinelService"

# Development-only or unused modules that must not ship in the program:
# * sentinel.sim opens a TCP listener (simulated camera) - development tool only (docs/07);
# * tkinter / test frameworks are never used by Sentinel.
EXCLUDES = ["sentinel.sim", "sentinel.sim.camera", "tkinter", "_tkinter", "pytest", "_pytest", "unittest"]

# Imported lazily (inside functions) or chosen at run time by platform; listed so the
# analysis never depends on how PyInstaller's bytecode scan treats function-level imports.
HIDDEN_IMPORTS = ["sentinel.platform.windows", "sentinel.platform.posix", "sentinel.launcher.cli",
                  "sentinel.service",
                  # Operator screen (launcher view, imported lazily); PyInstaller's PySide6 hooks then
                  # collect QtCore/QtGui/QtWidgets and the Qt platform plugins.
                  "sentinel.gui", "sentinel.gui.__main__", "sentinel.gui.window", "sentinel.power",
                  "sentinel.archive.__main__", "sentinel.export.__main__",
                  "sentinel.drives.cli"]
# The service executable (and the launcher's service control) import these lazily.
SERVICE_HIDDEN_IMPORTS = ["sentinel.winsvc", "sentinel.winsvc.__main__", "sentinel.winsvc.scm",
                          "sentinel.winsvc.commands", "sentinel.winsvc.control", "sentinel.winsvc.credstore",
                          "sentinel.winsvc.dpapi", "sentinel.winsvc.eventlog", "sentinel.winsvc.codeacl",
                          "sentinel.watchdog", "sentinel.platform.winacl"]


def version() -> str:
    text = (SRC / "sentinel" / "__init__.py").read_text("utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise RuntimeError("cannot read sentinel.__version__")
    return m.group(1)


def version_tuple() -> tuple[int, int, int, int]:
    nums = [int(n) for n in re.findall(r"\d+", version().split("dev")[0])][:3]
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2], 0)
