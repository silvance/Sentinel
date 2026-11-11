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
                  "sentinel.gui", "sentinel.gui.__main__", "sentinel.gui.app.window",
                  "sentinel.gui.app.status_pages", "sentinel.gui.app.settings_pages",
                  "sentinel.gui.app.drive_pages", "sentinel.gui.app.setup_page", "sentinel.power",
                  "sentinel.archive.__main__", "sentinel.export.__main__",
                  "sentinel.drives.cli",
                  # "Sentinel.exe ledger head|verify" (review 2026-11-10: the deployed build has no Python)
                  "sentinel.ledgerchain",
                  # "Sentinel.exe update ..." and update files dropped onto Sentinel.exe (2026-11-10)
                  "sentinel.update.cli", "sentinel.update.apply", "sentinel.update.package",
                  "sentinel.update.ed25519", "sentinel.update.keys"]

# LGPL-3.0 section 4(d)(1) (review 2026-11-10): the PySide6/shiboken6 Python packages are collected
# as .py files in _internal\ (not sealed in the PYZ inside Sentinel.exe), so a recipient can
# replace them together with the Qt DLLs, as SOURCE-OFFER.txt says. Passed to Analysis().
MODULE_COLLECTION_MODE = {"PySide6": "py", "shiboken6": "py"}

# Collected by PyInstaller's Qt hooks but never used by Sentinel (Widgets, raster painting, SVG
# and PNG icons only). Leaving them out keeps the licence set to what the source offer covers
# (review 2026-11-10): opengl32sw.dll is Mesa llvmpipe (MIT + LLVM) and the image-format plugins
# below come from the qtimageformats module (bundled libtiff/libwebp).
EXCLUDE_BINARIES = ("opengl32sw.dll", "qicns", "qtga", "qtiff", "qwbmp", "qwebp", "qjp2", "qmng", "qheif")


def _excluded(dest: str) -> bool:
    p = dest.replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    if name == "opengl32sw.dll":
        return True
    stem = name.split(".", 1)[0].removeprefix("lib")
    return "/plugins/imageformats/" in f"/{p}" and stem in EXCLUDE_BINARIES


def filter_binaries(toc: list) -> list:
    """PyInstaller TOC (dest_name, src_name, typecode) without the EXCLUDE_BINARIES entries."""
    return [entry for entry in toc if not _excluded(entry[0])]
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
