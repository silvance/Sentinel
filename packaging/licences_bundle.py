"""The ``licences/`` folder of the Sentinel build: third-party licence texts and the source offer.

Called by ``packaging/build.py`` after PyInstaller. Bundled third-party code:

* FFmpeg/FFprobe (``ffmpeg/``): separate programs, LGPL-3.0-or-later for a release build (licence
  also in ``ffmpeg/``). A static build (BtbN) also contains third-party libraries linked into
  the executables (LGPL, MPL-2.0, BSD, MIT, ISC, Apache-2.0, ...): they are named in
  ``THIRD-PARTY-NOTICES.txt`` and ``SOURCE-OFFER.txt``, and their licence files are copied from
  the source archive kept by ``packaging/fetch_sources.py`` (``notices/``) into
  ``licences/third-party/`` (review 2026-11-10).
* Qt and PySide6/shiboken6 (``_internal/PySide6``, ``_internal/shiboken6``): LGPL-3.0-only, used as
  replaceable DLLs and - since review 2026-11-10 - replaceable Python files (the spec collects the
  PySide6/shiboken6 packages as ``.py`` files instead of sealing them inside Sentinel.exe;
  LGPL-3.0 section 4(d)(1)). The wheels carry only the commercial licence file, so the LGPL-3.0
  and GPL-3.0 texts come from ``packaging/licences/`` (verbatim copies from gnu.org; the LGPL is a
  set of additional permissions on top of the GPL, so both must accompany the program). The Qt
  source modules named in the offer are derived from the files actually in the folder.
* Python and the libraries of its Windows build (OpenSSL, libffi, SQLite, ...): Python's own
  LICENSE file, which includes their notices.
* tzdata (Apache-2.0) and the PyInstaller bootloader (GPL-2.0 with the bootloader exception).

The source offer names exact upstream archives (GPL-3.0 section 6(d): "clear directions next to
the object code") and asks the distributing organisation to keep its own copy
(``packaging/fetch_sources.py``) so the offer can be honoured if upstream disappears. Anything
the build cannot vouch for is recorded (``notices_complete: false``) and shown by ``about``.
"""

from __future__ import annotations

import json
import shutil
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEXTS = HERE / "licences"
sys.path.insert(0, str(HERE))
import fetch_sources  # noqa: E402

# Distributions whose files end up in the program folder (PyInstaller) and need their notices.
BUNDLED_DISTS = {
    "pyside6-essentials": "LGPL-3.0-only (Qt for Python); see LGPL-3.0.txt and GPL-3.0.txt",
    "shiboken6": "LGPL-3.0-only (Qt for Python); see LGPL-3.0.txt and GPL-3.0.txt",
    "tzdata": "Apache-2.0",
    "pyinstaller": "GPL-2.0-or-later with the PyInstaller bootloader exception (the bootloader in "
                   "Sentinel.exe may be distributed under any terms)",
}
THIRD_PARTY_SUBDIR = "third-party"


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


def python_licence_candidates(base_prefix: str | None = None, stdlib: str | None = None) -> list[Path]:
    """Where CPython's licence file may be (review 2026-11-10: Linux builds failed here).

    python.org Windows installer: ``<prefix>\\LICENSE.txt``. Linux/pyenv/uv/conda builds install it
    with the standard library (``<prefix>/lib[64]/python3.X/LICENSE.txt``); distributions may move
    it to ``/usr/share/licenses`` or ``/usr/share/doc``."""
    base = Path(base_prefix or sys.base_prefix)
    std = Path(stdlib or sysconfig.get_paths()["stdlib"])
    ver = std.name                                    # "python3.14"
    return [base / "LICENSE.txt", base / "LICENSE", std / "LICENSE.txt", std / "LICENSE",
            base / "share" / "licenses" / ver / "LICENSE", base / "share" / "licenses" / "python3" / "LICENSE",
            base / "share" / "doc" / ver / "LICENSE", base / "share" / "doc" / ver / "copyright",
            base / "share" / "doc" / "python3" / "copyright"]


def find_python_licence(candidates: list[Path] | None = None) -> Path:
    cands = python_licence_candidates() if candidates is None else candidates
    for c in cands:
        if c.is_file():
            return c
    raise LicenceError("Python's licence file was not found; looked in: " + ", ".join(str(c) for c in cands))


def _app_files(app: Path) -> list[str] | None:
    internal = app / "_internal"
    if not internal.is_dir():
        return None
    return [p.relative_to(internal).as_posix() for p in internal.rglob("*") if p.is_file()]


def ffmpeg_section(ffmpeg_version: str, ffmpeg_ref: str, btbn_release: str | None, configuration: str,
                   ffmpeg_licence: str, sep: str) -> str:
    urls = fetch_sources.ffmpeg_source_urls(ffmpeg_ref)
    if fetch_sources.is_release_tag(ffmpeg_ref):
        src = f"  Source:          {urls[0]}\n                   (FFmpeg release {ffmpeg_ref[1:]}; also {urls[1]})\n"
    else:
        src = (f"  Source:          {urls[0]}\n"
               f"                   (git commit {ffmpeg_ref} of https://git.ffmpeg.org/ffmpeg.git)\n")
    libs = fetch_sources.external_libraries(configuration)
    text = (f"FFmpeg {ffmpeg_version} (ffmpeg{sep}ffmpeg, ffmpeg{sep}ffprobe; unmodified; {ffmpeg_licence})\n"
            + src)
    if btbn_release:
        text += ("  How it was built, and the exact versions of the libraries linked into it:\n"
                 f"                   https://github.com/BtbN/FFmpeg-Builds/tree/{btbn_release}\n"
                 f"                   (release https://github.com/BtbN/FFmpeg-Builds/releases/tag/{btbn_release};\n"
                 "                   each library's repository and commit is in scripts.d/*.sh)\n")
    else:
        text += ("  How it was built: the configure options below (printed by 'ffmpeg -version').\n"
                 f"                   {configuration or '(not recorded)'}\n")
    if libs:
        text += ("  Libraries linked into ffmpeg/ffprobe (each under its own licence: LGPL, MPL-2.0, BSD, MIT,\n"
                 "  ISC, Apache-2.0, ...; source of the exact versions included in this offer):\n"
                 + _wrap(", ".join(libs), "                   ") + "\n")
    return text


def _wrap(text: str, indent: str, width: int = 96) -> str:
    lines, cur = [], indent
    for word in text.split(" "):
        if len(cur) + len(word) + 1 > width and cur.strip():
            lines.append(cur.rstrip())
            cur = indent
        cur += word + " "
    lines.append(cur.rstrip())
    return "\n".join(lines)


def source_offer(ffmpeg_version: str, ffmpeg_commit: str, qt: str, btbn_release: str | None, contact: str | None,
                 *, ffmpeg_licence: str = "LGPL-3.0-or-later", configuration: str = "",
                 qt_modules: list[str] | None = None, pyside_python_replaceable: bool = True,
                 platform: str | None = None) -> str:
    win = (platform or sys.platform) == "win32"
    sep = "\\" if win else "/"
    who = contact or ("the organisation that gave you this program (ask the technical support contact who set "
                      "up this computer)")
    modules = qt_modules or fetch_sources.qt_modules_in(None)
    qt_urls = "\n".join(f"  {u}" for _, u, _ in fetch_sources.qt_archive_urls(qt, modules))
    gpl = ffmpeg_licence.startswith("GPL")
    header = ("!! THIS FOLDER CONTAINS A GPL FFMPEG BUILT FOR INTERNAL USE ONLY: it must NOT be given to anyone\n"
              "!! outside the organisation (see ffmpeg" + sep + "FFMPEG-LICENCE-NOTE.txt).\n\n") if gpl else ""
    if pyside_python_replaceable:
        replace = (f"you may replace the Qt/PySide6 DLLs and the PySide6/shiboken6 Python files in\n"
                   f"_internal{sep}PySide6 and _internal{sep}shiboken6 with your own builds of the same version\n"
                   "(Sentinel loads them from there; 'Sentinel about --verify' will then list them as changed).")
    else:
        replace = (f"you may replace the Qt/PySide6 DLLs in _internal{sep}PySide6 and _internal{sep}shiboken6 with\n"
                   "your own builds of the same version. NOTE: in this build the PySide6 Python modules are\n"
                   "inside the Sentinel program file; ask the contact below for the relinkable form.")
    return f"""\
{header}WHERE TO GET THE SOURCE CODE (LGPL and other open-source components of Sentinel)
================================================================================
This folder contains programs and libraries licensed under the GNU Lesser General Public License
version 3 (LGPL-3.0; texts: LGPL-3.0.txt and GPL-3.0.txt in this folder) and other open-source
licences (THIRD-PARTY-NOTICES.txt). You may obtain their complete corresponding source code, and
{replace}

{ffmpeg_section(ffmpeg_version, ffmpeg_commit, btbn_release, configuration, ffmpeg_licence, sep)}
Qt for Python (PySide6, shiboken6) {qt}
  https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{qt}-src/pyside-setup-everywhere-src-{qt}.tar.xz

Qt {qt} (the Qt libraries, plugins and translations in _internal{sep}PySide6; modules: {", ".join(modules)})
{qt_urls}

WRITTEN OFFER. For at least three years after you received this program, {who} will give you, on
request and for no more than the cost of physically providing it, a copy of the complete
corresponding source code of the components above, including the libraries linked into FFmpeg,
on a durable medium.
"""


def _copy_notices(notices_dir: Path | None, dest: Path, ffmpeg_ref: str, qt: str) -> tuple[list[str], list[str]]:
    """Copy fetch_sources' notices/ into licences/third-party/. Returns (copied files, problems)."""
    if notices_dir is None or not (notices_dir / fetch_sources.NOTICES_INDEX).is_file():
        return [], [f"no licence files of the third-party source archives ({notices_dir or 'none given'}); run "
                    "packaging/fetch_sources.py for this build and build again"]
    try:
        index = json.loads((notices_dir / fetch_sources.NOTICES_INDEX).read_text("utf-8"))
    except (OSError, ValueError):
        return [], [f"{notices_dir / fetch_sources.NOTICES_INDEX} is unreadable"]
    problems = []
    if index.get("ffmpeg_ref") != ffmpeg_ref or index.get("qt") != qt:
        return [], [f"the notices in {notices_dir} are for FFmpeg {index.get('ffmpeg_ref')} / Qt {index.get('qt')}, "
                    f"not FFmpeg {ffmpeg_ref} / Qt {qt}; run packaging/fetch_sources.py for this build"]
    if not index.get("complete"):
        problems.append(f"some sources could not be fetched (see SOURCES-MISSING.txt next to {notices_dir})")
    target = dest / THIRD_PARTY_SUBDIR
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(notices_dir, target)
    return sorted(p.relative_to(dest).as_posix() for p in target.rglob("*") if p.is_file()), problems


def write_licences(app: Path, ffmpeg_version: str, ffmpeg_commit: str, btbn_release: str | None,
                   contact: str | None = None, *, ffmpeg_licence: str = "LGPL-3.0-or-later",
                   ffmpeg_configuration: str = "", notices_dir: Path | None = None,
                   platform: str | None = None) -> dict:
    """Create ``app/licences``. Returns the manifest block. Raises LicenceError if a required text is missing."""
    platform = platform or sys.platform
    dest = app / "licences"
    dest.mkdir(exist_ok=True)
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt"):
        src = TEXTS / name
        if not src.is_file():
            raise LicenceError(f"{src} is missing; the build cannot ship Qt/PySide6/FFmpeg without it")
        shutil.copy2(src, dest / name)
    shutil.copy2(find_python_licence(), dest / "Python-LICENSE.txt")
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
    files = _app_files(app)
    qt_modules = fetch_sources.qt_modules_in(files)
    problems: list[str] = []
    low = [f.lower() for f in files or []]
    pyside_loose = files is None or "pyside6/__init__.py" in low
    if not pyside_loose:
        problems.append("the PySide6 Python modules are inside Sentinel.exe, not replaceable files "
                        "(spec_common.MODULE_COLLECTION_MODE not applied)")
    opengl_sw = any(f.endswith("opengl32sw.dll") for f in low)
    if opengl_sw:
        problems.append("opengl32sw.dll (Mesa llvmpipe, MIT; LLVM, Apache-2.0) is in the folder without its "
                        "notice (spec_common.EXCLUDE_BINARIES not applied)")
    third_party, notice_problems = _copy_notices(notices_dir, dest, ffmpeg_commit, qt)
    problems += notice_problems
    (dest / "SOURCE-OFFER.txt").write_text(
        source_offer(ffmpeg_version, ffmpeg_commit, qt, btbn_release, contact, ffmpeg_licence=ffmpeg_licence,
                     configuration=ffmpeg_configuration, qt_modules=qt_modules,
                     pyside_python_replaceable=pyside_loose, platform=platform), encoding="utf-8")
    libs = fetch_sources.external_libraries(ffmpeg_configuration)
    win = platform == "win32"
    lines = ["THIRD-PARTY SOFTWARE IN THIS FOLDER", "=" * 35, "",
             f"FFmpeg {ffmpeg_version}: {ffmpeg_licence} (ffmpeg{'\\' if win else '/'} folder; "
             "LGPL-3.0.txt, GPL-3.0.txt)."]
    if libs:
        lines.append("  Libraries linked into ffmpeg and ffprobe, each under its own licence (LGPL, MPL-2.0, BSD, "
                     "MIT, ISC, Apache-2.0, ...): " + ", ".join(libs) + ".")
        if "mingw" in ffmpeg_configuration:
            lines.append("  Also the GCC runtime (libgcc, libstdc++, libgomp; GPL-3.0 with the GCC Runtime Library "
                         "Exception) and the mingw-w64 runtime and winpthreads (ZPL/MIT/BSD-style).")
        lines.append(f"  Their licence and copyright notices: {THIRD_PARTY_SUBDIR}/ (one folder per source archive)."
                     if third_party else "  THEIR LICENCE NOTICES ARE NOT INCLUDED IN THIS BUILD (see below).")
    lines.append(f"Qt {qt} ({', '.join(qt_modules)}): LGPL-3.0-only; Qt's bundled third-party code "
                 "(zlib, libpng, libjpeg, HarfBuzz, FreeType, PCRE2, ...) "
                 + (f"is listed with its notices in {THIRD_PARTY_SUBDIR}/ (qt_attribution.json and the licence "
                    "files of each Qt module)." if third_party else "- ITS NOTICES ARE NOT INCLUDED (see below)."))
    if win:
        lines.append(f"Python {sys.version.split()[0]} and the libraries of its Windows build (OpenSSL, libffi, "
                     "SQLite, zlib, ...): PSF licence and the notices in Python-LICENSE.txt.")
    else:
        lines.append(f"Python {sys.version.split()[0]}: PSF licence (Python-LICENSE.txt). Shared libraries copied "
                     "from the build system into _internal/ (e.g. OpenSSL, libffi, SQLite) keep their own "
                     "licences, as packaged by that distribution.")
    for name, licence in BUNDLED_DISTS.items():
        files_s = ", ".join(copied[name]) or "no licence file in the package; licence named here"
        lines.append(f"{name} {_version(name)}: {licence}. Files: {files_s}.")
    lines += ["", "Source code of the LGPL components and of the libraries linked into FFmpeg: SOURCE-OFFER.txt.",
              "Sentinel itself: see the project's licence (not covered by this file)."]
    if problems:
        lines += ["", "INCOMPLETE - this folder must not be given to anyone outside the organisation until "
                      "these are fixed:"] + [f"  * {p}" for p in problems]
    (dest / "THIRD-PARTY-NOTICES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"folder": "licences", "qt_for_python": qt, "qt_modules": qt_modules, "ffmpeg_commit": ffmpeg_commit,
            "ffmpeg_license": ffmpeg_licence, "btbn_release": btbn_release, "ffmpeg_libraries": libs,
            "pyside6_python": "replaceable .py files" if pyside_loose else "inside Sentinel.exe",
            "third_party_notices": len(third_party), "notices_complete": not problems, "notices_problems": problems,
            "source_offer_contact": contact or "not set (generic wording)",
            "files": sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())}
