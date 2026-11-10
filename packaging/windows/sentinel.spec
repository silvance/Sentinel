# -*- mode: python -*-
# PyInstaller spec: Sentinel for Windows, ONE-FOLDER build (docs/08_PACKAGING.md explains why
# not one-file). Run through packaging/build.py, which also bundles FFmpeg and writes the
# build manifest:
#
#   .venv-build\Scripts\python packaging\build.py --ffmpeg-dir <folder with ffmpeg.exe, ffprobe.exe>
#
# Output: dist\Sentinel\Sentinel.exe + dist\Sentinel\_internal\ (+ ffmpeg\ added by build.py)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH).resolve().parents[0]))  # packaging/ (for spec_common)
import spec_common as common  # noqa: E402

from PyInstaller.utils.win32.versioninfo import (  # noqa: E402
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo)

ver = common.version_tuple()
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=ver, prodvers=ver, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1,
                      subtype=0x0, date=(0, 0)),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("ProductName", "Sentinel"),
            StringStruct("FileDescription", "Sentinel camera recorder"),
            StringStruct("FileVersion", common.version()),
            StringStruct("ProductVersion", common.version()),
            StringStruct("InternalName", "Sentinel"),
            StringStruct("OriginalFilename", "Sentinel.exe"),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

a = Analysis(
    [str(common.ENTRY)],
    pathex=[str(common.SRC)],
    binaries=[],
    datas=[],
    hiddenimports=common.HIDDEN_IMPORTS,
    hookspath=[],
    runtime_hooks=[],
    excludes=common.EXCLUDES,
    noarchive=False,
    optimize=0,           # keep asserts and docstrings: the code relies on neither, but -O changes behaviour silently
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # one-folder: libraries live in _internal\, nothing is unpacked at start
    name="Sentinel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX-packed executables are a common antivirus false-positive trigger
    console=True,            # the status banner is shown in this console window (GUI branch replaces it)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
    # icon=...               # no approved icon yet; add packaging/windows/sentinel.ico when one exists
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Sentinel",
)
