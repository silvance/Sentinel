# -*- mode: python -*-
# PyInstaller spec: Sentinel for Linux, ONE-FOLDER build (same design as the Windows spec).
# Must be built ON Linux (PyInstaller does not cross-compile), with the oldest glibc you need
# to support (the program runs on that glibc version or newer):
#
#   packaging/linux/build.sh --ffmpeg-dir <folder with ffmpeg, ffprobe>
#
# Output: dist/Sentinel/sentinel + dist/Sentinel/_internal/ (+ ffmpeg/ and sentinel.desktop)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH).resolve().parents[0]))  # packaging/ (for spec_common)
import spec_common as common  # noqa: E402

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
    module_collection_mode=common.MODULE_COLLECTION_MODE,   # PySide6 as replaceable .py files (LGPL)
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sentinel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(exe, common.filter_binaries(a.binaries), a.datas, strip=False, upx=False, upx_exclude=[], name="Sentinel")
