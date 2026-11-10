"""The ``licences/`` folder of the Sentinel build: third-party licence texts and the LGPL source offer.

Called by ``packaging/build.py`` after PyInstaller. Bundled third-party code:

* FFmpeg/FFprobe (``ffmpeg/``): LGPL-3.0-or-later, separate programs (licence also in ``ffmpeg/``).
* Qt and PySide6/shiboken6 (``_internal/PySide6``, ``_internal/shiboken6``): LGPL-3.0-only, used as
  DLLs that can be replaced (LGPL-3.0 section 4(d)(1)). The wheels carry only the commercial
  licence file, so the LGPL-3.0 and GPL-3.0 texts come from ``packaging/licences/`` (verbatim
  copies from gnu.org; the LGPL is a set of additional permissions on top of the GPL, so both
  must accompany the program).
* Python and the libraries of its Windows build (OpenSSL, libffi, SQLite, ...): Python's own
  LICENSE file, which includes their notices.
* tzdata (Apache-2.0) and the PyInstaller bootloader (GPL-2.0 with the bootloader exception).

The source offer names exact upstream archives (GPL-3.0 section 6(d): "clear directions next to
the object code") and asks the distributing organisation to keep its own copy
(``packaging/fetch_sources.py``) so the offer can be honoured if upstream disappears.
"""

from __future__ import annotations

import shutil
import sys
from importlib import metadata
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEXTS = HERE / "licences"

# Distributions whose files end up in the program folder (PyInstaller) and need their notices.
BUNDLED_DISTS = {
    "pyside6-essentials": "LGPL-3.0-only (Qt for Python); see LGPL-3.0.txt and GPL-3.0.txt",
    "shiboken6": "LGPL-3.0-only (Qt for Python); see LGPL-3.0.txt and GPL-3.0.txt",
    "tzdata": "Apache-2.0",
    "pyinstaller": "GPL-2.0-or-later with the PyInstaller bootloader exception (the bootloader in "
                   "Sentinel.exe may be distributed under any terms)",
}


class LicenceError(RuntimeError):
    pass


def _dist_licence_files(name: str) -> list[Path]:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return []
    out = []
    for f in dist.files or []:
        p = Path(str(f))
        if "Commercial" in p.name:
            # The Qt wheels' only licence file is Qt's commercial licence; Sentinel uses Qt under
            # the LGPL, so shipping that file would wrongly suggest a commercial licence.
            continue
        if "licenses" in p.parts or p.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
            located = Path(str(dist.locate_file(f)))
            if located.is_file():
                out.append(located)
    return out


def _version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not installed"


def source_offer(ffmpeg_version: str, ffmpeg_commit: str, qt: str, btbn_release: str, contact: str | None) -> str:
    mm = ".".join(qt.split(".")[:2])
    qt_sub = f"https://download.qt.io/official_releases/qt/{mm}/{qt}/submodules"
    who = contact or ("the organisation that gave you this program (ask the technical support contact who set "
                      "up this computer)")
    return f"""\
WHERE TO GET THE SOURCE CODE (LGPL components of Sentinel)
==========================================================
This folder contains programs and libraries licensed under the GNU Lesser General Public License
version 3 (LGPL-3.0; texts: LGPL-3.0.txt and GPL-3.0.txt in this folder). You may obtain their
complete corresponding source code, and you may replace the Qt/PySide6 DLLs in
_internal\\PySide6 and _internal\\shiboken6 with your own builds of the same version.

FFmpeg {ffmpeg_version} (ffmpeg\\ffmpeg.exe, ffmpeg\\ffprobe.exe; unmodified)
  Source:          https://github.com/FFmpeg/FFmpeg/archive/{ffmpeg_commit}.tar.gz
                   (git commit {ffmpeg_commit} of https://git.ffmpeg.org/ffmpeg.git)
  How it was built, and the exact versions of the libraries linked into it:
                   https://github.com/BtbN/FFmpeg-Builds/tree/{btbn_release}
                   (release https://github.com/BtbN/FFmpeg-Builds/releases/tag/{btbn_release})

Qt for Python (PySide6, shiboken6) {qt}
  https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{qt}-src/pyside-setup-everywhere-src-{qt}.tar.xz

Qt {qt} (Qt6Core, Qt6Gui, Qt6Widgets, Qt6Network, Qt6Svg, plugins, translations)
  {qt_sub}/qtbase-everywhere-src-{qt}.tar.xz
  {qt_sub}/qtsvg-everywhere-src-{qt}.tar.xz
  {qt_sub}/qttranslations-everywhere-src-{qt}.tar.xz

WRITTEN OFFER. For at least three years after you received this program, {who} will give you, on
request and for no more than the cost of physically providing it, a copy of the complete
corresponding source code of the components above on a durable medium.
"""


def write_licences(app: Path, ffmpeg_version: str, ffmpeg_commit: str, btbn_release: str,
                   contact: str | None = None) -> dict:
    """Create ``app/licences``. Returns the manifest block. Raises LicenceError if a required text is missing."""
    dest = app / "licences"
    dest.mkdir(exist_ok=True)
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt"):
        src = TEXTS / name
        if not src.is_file():
            raise LicenceError(f"{src} is missing; the build cannot ship Qt/PySide6/FFmpeg without it")
        shutil.copy2(src, dest / name)
    py_licence = Path(sys.base_prefix) / "LICENSE.txt"
    if not py_licence.is_file():
        py_licence = Path(sys.base_prefix) / "LICENSE"
    if not py_licence.is_file():
        raise LicenceError(f"Python's licence file was not found under {sys.base_prefix}")
    shutil.copy2(py_licence, dest / "Python-LICENSE.txt")
    copied = {}
    for name in BUNDLED_DISTS:
        files = _dist_licence_files(name)
        copied[name] = []
        for f in files:
            target = dest / f"{name}-{_version(name)}-{f.name}"
            shutil.copy2(f, target)
            copied[name].append(target.name)
    qt = _version("pyside6-essentials")
    if qt == "not installed":
        raise LicenceError("PySide6-Essentials is not installed in the build environment")
    (dest / "SOURCE-OFFER.txt").write_text(source_offer(ffmpeg_version, ffmpeg_commit, qt, btbn_release, contact),
                                           encoding="utf-8")
    lines = ["THIRD-PARTY SOFTWARE IN THIS FOLDER", "=" * 35, "",
             f"FFmpeg {ffmpeg_version}: LGPL-3.0-or-later (ffmpeg\\ folder; LGPL-3.0.txt, GPL-3.0.txt).",
             f"Python {sys.version.split()[0]} and the libraries of its Windows build (OpenSSL, libffi, SQLite, "
             "zlib, ...): PSF licence and the notices in Python-LICENSE.txt."]
    for name, licence in BUNDLED_DISTS.items():
        files = ", ".join(copied[name]) or "no licence file in the package; licence named here"
        lines.append(f"{name} {_version(name)}: {licence}. Files: {files}.")
    lines += ["", "Source code of the LGPL components: SOURCE-OFFER.txt.",
              "Sentinel itself: see the project's licence (not covered by this file)."]
    (dest / "THIRD-PARTY-NOTICES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"folder": "licences", "qt_for_python": qt, "ffmpeg_commit": ffmpeg_commit, "btbn_release": btbn_release,
            "source_offer_contact": contact or "not set (generic wording)", "files": sorted(p.name for p in dest.iterdir())}
