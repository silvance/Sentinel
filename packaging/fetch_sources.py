"""Download and keep the source code of the third-party components shipped in ``dist/Sentinel``.

    python packaging/fetch_sources.py [--manifest dist/Sentinel/BUILD-MANIFEST.json] [--out third_party/sources]
    python packaging/fetch_sources.py --list        # only print what would be fetched

The Sentinel folder contains LGPL-licensed FFmpeg (separate programs) and Qt/PySide6 (DLLs and
replaceable Python files). A static FFmpeg build such as BtbN's also has dozens of third-party
libraries linked into ``ffmpeg.exe`` (LGPL, MPL-2.0, BSD, MIT, ISC, Apache-2.0, ...). Whoever
gives that folder to someone else must make the matching source available
(``licences/SOURCE-OFFER.txt`` in the folder says where) and ship those libraries' notices.

This script fetches exactly the versions named in the build manifest:

* FFmpeg at its git commit (or release tag), the BtbN build scripts at the release tag, and -
  read from those scripts - every library they pin for the LGPL variant (git commit / svn
  revision; a superset: scripts for other targets are included too);
* PySide6/shiboken6 and the Qt modules whose files are in the program folder.

It records their SHA-256 in ``SOURCES-SHA256.txt``, copies each archive's licence/notice files
into ``notices/`` (the build puts that folder into ``licences/third-party/``) and is safe to
re-run (files already present are kept). Sources that need ``git``/``svn`` are fetched with them
when installed; anything that could not be fetched is listed in ``SOURCES-MISSING.txt`` and the
script exits with status 1. Keep the folder for at least three years after the last copy of that
build was handed out.

The archives are not part of the program folder and are never committed (``third_party/`` is
git-ignored). Nothing here runs at build time: the build itself never downloads anything.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# BtbN FFmpeg-Builds release the bundled FFmpeg came from (docs/08 §8.6).
BTBN_RELEASE = "autobuild-2026-11-10-12-39"
NOTICES_DIR = "notices"
NOTICES_INDEX = "NOTICES.json"

# Qt modules whose code can end up in the program folder, and how to recognise each one there
# (paths relative to _internal/PySide6, lower case). qtbase and qttranslations are always used.
QT_MODULE_MARKERS = {
    "qtsvg": ("qt6svg.dll", "libqt6svg.so.6", "plugins/iconengines/qsvgicon", "plugins/imageformats/qsvg"),
    "qtimageformats": tuple(f"plugins/imageformats/{p}" for p in
                            ("qicns", "qtga", "qtiff", "qwbmp", "qwebp", "qjp2", "qmng", "qheif")),
}


# ---------------------------------------------------------------------- FFmpeg reference
def ffmpeg_source_ref(version: str) -> str:
    """Source reference of an FFmpeg version string (review 2026-11-10: also release builds).

    ``n9.0.2-48-gd4070a14ce-20261110`` -> ``d4070a14ce`` (git commit); ``7.1.2`` or ``n7.1.2`` ->
    ``n7.1.2`` (FFmpeg's release tags are ``n<version>``); ``9.0.1-full_build-www.gyan.dev`` -> ``n9.0.1``."""
    m = re.search(r"-g([0-9a-f]{7,40})(?:-|$)", version)
    if m:
        return m.group(1)
    head = version.split("-")[0]
    return head if head.startswith("n") else f"n{head}"


def ffmpeg_commit(version: str) -> str:
    """``n9.0.2-48-gd4070a14ce-20261110`` -> ``d4070a14ce`` (FFmpeg git commit of the build)."""
    m = re.search(r"-g([0-9a-f]{7,40})(?:-|$)", version)
    if not m:
        raise SystemExit(f"cannot find the FFmpeg git commit in version {version!r}")
    return m.group(1)


def is_release_tag(ref: str) -> bool:
    return bool(re.fullmatch(r"n\d+(\.\d+)*", ref))


def ffmpeg_source_urls(ref: str) -> list[str]:
    """Where the FFmpeg source of ``ref`` can be downloaded (official tarball first for releases)."""
    if is_release_tag(ref):
        return [f"https://ffmpeg.org/releases/ffmpeg-{ref[1:]}.tar.xz",
                f"https://github.com/FFmpeg/FFmpeg/archive/refs/tags/{ref}.tar.gz"]
    return [f"https://github.com/FFmpeg/FFmpeg/archive/{ref}.tar.gz"]


def is_btbn_build(configuration: str) -> bool:
    """BtbN FFmpeg-Builds are configured inside their container with ``--prefix=/ffbuild/prefix``."""
    return "--prefix=/ffbuild/" in configuration


# Libraries FFmpeg can link that are not named --enable-lib*.
_OTHER_LIBRARY_FLAGS = ("zlib", "iconv", "gmp", "lzma", "fontconfig", "lcms2", "sdl2", "openal", "opencl",
                        "vulkan", "chromaprint", "lv2", "amf", "ffnvcodec", "cuda-llvm", "vaapi", "vapoursynth",
                        "gnutls", "openssl", "mbedtls", "schannel", "bzlib")


def external_libraries(configuration: str) -> list[str]:
    """Names of the external libraries/APIs enabled in an FFmpeg ``configuration:`` line."""
    flags = configuration.split()
    out = [f[len("--enable-lib"):] for f in flags if f.startswith("--enable-lib")]
    out += [f[len("--enable-"):] for f in flags if f[len("--enable-"):] in _OTHER_LIBRARY_FLAGS
            and f.startswith("--enable-")]
    return sorted(dict.fromkeys(out))


# ---------------------------------------------------------------------- BtbN dependency pins
class Dependency(NamedTuple):   # not a dataclass: the tests load this file without registering it
    script: str          # scripts.d path, e.g. "50-libssh.sh"
    repo: str
    ref: str             # git commit or tag / svn revision
    kind: str            # "git" | "svn"

    @property
    def name(self) -> str:
        parts = [p[:-4] if p.endswith(".git") else p for p in PurePosixPath(self.repo.rstrip("/")).parts]
        while len(parts) > 1 and parts[-1] in ("code", "git", "svn", "trunk"):   # sourceforge: p/<project>/code
            parts.pop()
        return parts[-1]

    @property
    def file_name(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{self.name}-{self.ref}")
        return f"btbn-dep-{safe}.tar.gz"


_LGPL_EXCLUDED = re.compile(r"\[\[\s*\$VARIANT\s*==\s*lgpl\*\s*\]\]\s*&&\s*return\s+-1")
_NONFREE_ONLY = re.compile(r"\[\[\s*\$VARIANT\s*==\s*nonfree\*\s*\]\]\s*\|\|\s*return\s+-1")


def parse_btbn_script(rel: str, text: str) -> list[Dependency]:
    """Pinned sources of one BtbN ``scripts.d`` script; [] if it is never built for the LGPL variant."""
    if re.search(r'^SCRIPT_SKIP="?1', text, re.M):
        return []
    enabled = re.search(r"ffbuild_enabled\(\)\s*\{(.*?)\n\}", text, re.S)
    body = enabled.group(1) if enabled else ""
    if _LGPL_EXCLUDED.search(body) or _NONFREE_ONLY.search(body):
        return []    # GPL-only or nonfree-only library: not in an LGPL build
    values = dict(re.findall(r'^(SCRIPT_[A-Z0-9_]+)="?([^"\n]*)"?\s*$', text, re.M))
    deps = []
    for n in ("", "2", "3", "4", "5"):
        repo = values.get(f"SCRIPT_REPO{n}")
        if not repo:
            continue
        if f"SCRIPT_REV{n}" in values:
            deps.append(Dependency(rel, repo, values[f"SCRIPT_REV{n}"], "svn"))
        elif f"SCRIPT_COMMIT{n}" in values:
            deps.append(Dependency(rel, repo, values[f"SCRIPT_COMMIT{n}"], "git"))
        else:
            deps.append(Dependency(rel, repo, values.get(f"SCRIPT_BRANCH{n}", "unpinned"), "git"))
    return deps


def btbn_dependencies(scripts_tar: Path) -> list[Dependency]:
    """Every library source pinned by the BtbN build scripts for the LGPL variant (all targets)."""
    out: list[Dependency] = []
    with tarfile.open(scripts_tar) as tar:
        for m in sorted(tar.getmembers(), key=lambda m: m.name):
            parts = PurePosixPath(m.name).parts
            if not m.isfile() or "scripts.d" not in parts or not m.name.endswith(".sh"):
                continue
            rel = "/".join(parts[parts.index("scripts.d") + 1:])
            fh = tar.extractfile(m)
            if fh is not None:
                out += parse_btbn_script(rel, fh.read().decode("utf-8", "replace"))
    unique = {(d.repo, d.ref): d for d in out}
    return sorted(unique.values(), key=lambda d: (d.name.lower(), d.ref))


_GITLAB_HOSTS = ("gitlab.com", "gitlab.freedesktop.org", "code.videolan.org", "gitlab.gnome.org",
                 "gitlab.xiph.org")


def dependency_archive_url(dep: Dependency) -> str | None:
    """A tarball URL for ``dep`` at its pinned ref, or None if it needs git/svn."""
    if dep.kind != "git" or dep.ref == "unpinned":
        return None
    m = re.fullmatch(r"https://([^/]+)/(.+?)(?:\.git)?/?", dep.repo)
    if not m:
        return None
    host, path = m.groups()
    if host == "github.com":
        return f"https://github.com/{path}/archive/{dep.ref}.tar.gz"
    if host in _GITLAB_HOSTS:
        return f"https://{host}/{path}/-/archive/{dep.ref}/{PurePosixPath(path).name}-{dep.ref}.tar.gz"
    if host.endswith(".googlesource.com"):
        return f"https://{host}/{path}/+archive/{dep.ref}.tar.gz"
    return None


def checkout_command(dep: Dependency) -> str:
    if dep.kind == "svn":
        return f"svn export -r {dep.ref} {dep.repo}"
    return f"git clone {dep.repo} {dep.name} && git -C {dep.name} archive --format=tar.gz {dep.ref}"


# ---------------------------------------------------------------------- Qt / PySide6
def qt_version(manifest: dict) -> str:
    for entry in manifest.get("build_environment", []):
        name, _, ver = entry.partition("==")
        if name.lower() in ("pyside6-essentials", "pyside6_essentials"):
            return ver
    raise SystemExit("the build manifest does not name PySide6-Essentials")


def qt_modules_in(files: list[str] | None) -> list[str]:
    """Qt source modules whose code is in the program folder (from relative file paths).

    ``None`` (folder not known) gives the modules of the standard Sentinel build."""
    mods = ["qtbase", "qttranslations"]
    if files is None:
        return [*mods, "qtsvg"]
    low = [f.replace("\\", "/").lower() for f in files]
    for mod, markers in QT_MODULE_MARKERS.items():
        if any(f"pyside6/{mk}" in p for p in low for mk in markers):
            mods.append(mod)
    return mods


def qt_archive_urls(qt: str, modules: list[str]) -> list[tuple[str, str, str]]:
    mm = ".".join(qt.split(".")[:2])
    qt_sub = f"https://download.qt.io/official_releases/qt/{mm}/{qt}/submodules"
    what = {"qtbase": "Core, Gui, Widgets, Network and the platform/style/TLS plugins",
            "qtsvg": "Svg and the SVG icon/image plugins", "qttranslations": "translations",
            "qtimageformats": "TIFF/WebP/ICNS/TGA/WBMP image plugins"}
    return [(f"{m}-everywhere-src-{qt}.tar.xz", f"{qt_sub}/{m}-everywhere-src-{qt}.tar.xz",
             f"Qt {qt} {what.get(m, m)}") for m in modules]


# ---------------------------------------------------------------------- the list
def source_list(manifest: dict, btbn_release: str | None = BTBN_RELEASE) -> list[tuple[str, str, str]]:
    """(file name, URL, what it is) for every source archive the offer refers to directly.

    The libraries pinned by the BtbN scripts are added by :func:`main` after the scripts archive
    is present (:func:`btbn_dependencies`)."""
    ref = ffmpeg_source_ref(manifest["ffmpeg"]["version"])
    url = ffmpeg_source_urls(ref)[0]
    items = [(f"ffmpeg-{ref}" + (".tar.xz" if url.endswith(".tar.xz") else ".tar.gz"), url,
              f"FFmpeg source at {'release ' if is_release_tag(ref) else 'git commit '}{ref} (the bundled ffmpeg/ffprobe)")]
    if btbn_release:
        items.append((f"FFmpeg-Builds-{btbn_release}.tar.gz",
                      f"https://github.com/BtbN/FFmpeg-Builds/archive/refs/tags/{btbn_release}.tar.gz",
                      "BtbN build scripts: how FFmpeg was configured and which library versions were linked"))
    qt = qt_version(manifest)
    items.append((f"pyside-setup-everywhere-src-{qt}.tar.xz",
                  f"https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{qt}-src/"
                  f"pyside-setup-everywhere-src-{qt}.tar.xz", f"PySide6 and shiboken6 {qt} source"))
    files = manifest.get("files")
    items += qt_archive_urls(qt, qt_modules_in(list(files) if isinstance(files, dict) else None))
    return items


def btbn_release_of(manifest: dict) -> str | None:
    """The BtbN release recorded by the build (None: not a BtbN FFmpeg)."""
    lic = manifest.get("third_party_licences") or {}
    if "btbn_release" in lic:
        return lic["btbn_release"] or None
    return BTBN_RELEASE if is_btbn_build(manifest.get("ffmpeg", {}).get("configuration", "")) else None


# ---------------------------------------------------------------------- download / notices
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".partial")
    req = urllib.request.Request(url, headers={"User-Agent": "sentinel-source-archive"})
    with urllib.request.urlopen(req, timeout=120) as r, tmp.open("wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.replace(dest)


def checkout(dep: Dependency, dest: Path) -> bool:
    """Archive ``dep`` with git/svn into ``dest`` (a .tar.gz). False if the tool is missing or it failed."""
    tool = "svn" if dep.kind == "svn" else "git"
    if shutil.which(tool) is None or dep.ref == "unpinned":
        return False
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / dep.name
        try:
            if dep.kind == "svn":
                subprocess.run(["svn", "export", "-q", "-r", dep.ref, dep.repo, str(work)], check=True, timeout=1800)
                with tarfile.open(dest.with_name(dest.name + ".partial"), "w:gz") as tar:
                    tar.add(work, arcname=f"{dep.name}-{dep.ref}")
            else:
                subprocess.run(["git", "clone", "-q", "--no-checkout", dep.repo, str(work)], check=True, timeout=1800)
                subprocess.run(["git", "-C", str(work), "archive", "--format=tar.gz", f"--prefix={dep.name}-{dep.ref}/",
                                "-o", str(dest.with_name(dest.name + ".partial")), dep.ref], check=True, timeout=600)
        except (OSError, subprocess.SubprocessError):
            dest.with_name(dest.name + ".partial").unlink(missing_ok=True)
            return False
    dest.with_name(dest.name + ".partial").replace(dest)
    return True


_NOTICE_NAMES = ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*", "COPYRIGHT*", "qt_attribution.json")


def extract_notices(archive: Path, dest: Path, max_bytes: int = 1 << 20) -> list[str]:
    """Copy the licence/notice files of a source archive into ``dest`` (no code is extracted).

    Taken: top-level LICENSE/COPYING/NOTICE/COPYRIGHT files, everything in a top-level LICENSES/
    folder (REUSE layout, used by Qt), and the same names plus ``qt_attribution.json`` anywhere
    below a ``3rdparty`` folder (Qt's bundled libraries)."""
    written: list[str] = []
    try:
        tar = tarfile.open(archive)
    except (tarfile.TarError, OSError):
        return written
    with tar:
        for m in tar.getmembers():
            if not m.isfile() or m.size > max_bytes:
                continue
            parts = PurePosixPath(m.name).parts
            if any(p in ("..", "") for p in parts) or m.name.startswith("/"):
                continue
            inner = parts[1:] if len(parts) > 1 else parts     # drop the archive's top folder
            base = inner[-1] if inner else ""
            named = any(fnmatch.fnmatch(base.upper(), pat.upper()) for pat in _NOTICE_NAMES)
            take = (len(inner) == 1 and named) or (len(inner) == 2 and inner[0] == "LICENSES") \
                or ("3rdparty" in inner[:-1] and named)
            if not take:
                continue
            fh = tar.extractfile(m)
            if fh is None:
                continue
            target = dest.joinpath(*inner)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(fh.read())
            written.append("/".join(inner))
    return sorted(written)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", default=str(ROOT / "dist" / "Sentinel" / "BUILD-MANIFEST.json"))
    ap.add_argument("--out", default=str(ROOT / "third_party" / "sources"))
    ap.add_argument("--btbn-release", default=None,
                    help="BtbN release the FFmpeg came from (default: as recorded in the build manifest)")
    ap.add_argument("--list", action="store_true", help="only print what would be fetched")
    args = ap.parse_args(argv)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    btbn = args.btbn_release or btbn_release_of(manifest)
    items = source_list(manifest, btbn)
    out = Path(args.out)
    scripts_tar = out / f"FFmpeg-Builds-{btbn}.tar.gz" if btbn else None
    if args.list:
        for name, url, what in items:
            print(f"{name}\n    {url}\n    {what}")
        if scripts_tar is not None:
            if scripts_tar.is_file():
                for dep in btbn_dependencies(scripts_tar):
                    print(f"{dep.file_name}\n    {dependency_archive_url(dep) or checkout_command(dep)}\n"
                          f"    library pinned by BtbN scripts.d/{dep.script}")
            else:
                print("(+ every library pinned by the BtbN scripts; listed once the scripts archive is present)")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    lines, missing, sources = [], [], []
    notices = out / NOTICES_DIR

    def keep(name: str, url: str | None, what: str, dep: Dependency | None = None) -> None:
        dest = out / name
        if not dest.exists():
            ok = False
            if url:
                print(f"downloading {url}")
                try:
                    fetch(url, dest)
                    ok = True
                except OSError as exc:
                    print(f"  failed: {exc}")
            if not ok and dep is not None:
                print(f"checking out {dep.repo} at {dep.ref}")
                ok = checkout(dep, dest)
            if not ok:
                how = url or (checkout_command(dep) if dep else "no download location")
                missing.append(f"{name}: {how}  ({what})")
                return
        digest = sha256(dest)
        print(f"{digest}  {name}  ({dest.stat().st_size / 1e6:.1f} MB)")
        lines.append(f"{digest}  {name}\n")
        sources.append({"file": name, "url": url or (checkout_command(dep) if dep else None), "what": what,
                        "notices": extract_notices(dest, notices / Path(name).name.split(".tar")[0])})

    for name, url, what in items:
        keep(name, url, what)
    deps: list[Dependency] = []
    if scripts_tar is not None and scripts_tar.is_file():
        deps = btbn_dependencies(scripts_tar)
        for dep in deps:
            keep(dep.file_name, dependency_archive_url(dep), f"{dep.name} (BtbN scripts.d/{dep.script})", dep)
    (out / "SOURCES-SHA256.txt").write_text("".join(lines), encoding="utf-8")
    for_build = {"version": manifest.get("version"), "git_commit": manifest.get("git_commit"),
                 "built_utc": manifest.get("built_utc"),
                 "ffmpeg_ref": ffmpeg_source_ref(manifest["ffmpeg"]["version"]),
                 "qt": qt_version(manifest), "btbn_release": btbn}
    (out / "SOURCES.json").write_text(json.dumps({"for_build": for_build, "sources": sources,
                                                  "missing": missing}, indent=2) + "\n", encoding="utf-8")
    # The build copies notices/ into licences/third-party/ only when this index says it is complete
    # for the same FFmpeg and Qt (review 2026-11-10: notices of the statically linked libraries).
    notices.mkdir(exist_ok=True)
    (notices / NOTICES_INDEX).write_text(json.dumps({**for_build, "complete": not missing,
                                                     "libraries": [d.name for d in deps]}, indent=2) + "\n",
                                         encoding="utf-8")
    missing_file = out / "SOURCES-MISSING.txt"
    if missing:
        missing_file.write_text("".join(f"{m}\n" for m in missing), encoding="utf-8")
        print(f"NOT COMPLETE: {len(missing)} source(s) could not be fetched; see {missing_file}", file=sys.stderr)
        return 1
    missing_file.unlink(missing_ok=True)
    print(f"kept in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
