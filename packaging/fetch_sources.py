"""Download and keep the source code of the LGPL components shipped in ``dist/Sentinel``.

    python packaging/fetch_sources.py [--manifest dist/Sentinel/BUILD-MANIFEST.json] [--out third_party/sources]

The Sentinel folder contains LGPL-licensed FFmpeg (separate programs) and Qt/PySide6 (DLLs).
Whoever gives that folder to someone else must make the matching source available
(``licences/SOURCE-OFFER.txt`` in the folder says where). This script fetches exactly the
versions named in the build manifest, records their SHA-256 in ``SOURCES-SHA256.txt`` and is
safe to re-run (files already present and unchanged are kept). Keep the folder for at least
three years after the last copy of that build was handed out.

The archives are not part of the program folder and are never committed (``third_party/`` is
git-ignored). Nothing here runs at build time: the build itself never downloads anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# BtbN FFmpeg-Builds release the bundled FFmpeg came from (docs/08 §8.6).
BTBN_RELEASE = "autobuild-2026-11-10-12-39"


def ffmpeg_commit(version: str) -> str:
    """``n9.0.2-48-gd4070a14ce-20261110`` -> ``d4070a14ce`` (FFmpeg git commit of the build)."""
    m = re.search(r"-g([0-9a-f]{7,40})(?:-|$)", version)
    if not m:
        raise SystemExit(f"cannot find the FFmpeg git commit in version {version!r}")
    return m.group(1)


def qt_version(manifest: dict) -> str:
    for entry in manifest.get("build_environment", []):
        name, _, ver = entry.partition("==")
        if name.lower() in ("pyside6-essentials", "pyside6_essentials"):
            return ver
    raise SystemExit("the build manifest does not name PySide6-Essentials")


def source_list(manifest: dict, btbn_release: str = BTBN_RELEASE) -> list[tuple[str, str, str]]:
    """(file name, URL, what it is) for every source archive the offer refers to."""
    commit = ffmpeg_commit(manifest["ffmpeg"]["version"])
    qt = qt_version(manifest)
    mm = ".".join(qt.split(".")[:2])
    qt_sub = f"https://download.qt.io/official_releases/qt/{mm}/{qt}/submodules"
    return [
        (f"ffmpeg-{commit}.tar.gz", f"https://github.com/FFmpeg/FFmpeg/archive/{commit}.tar.gz",
         f"FFmpeg source at git commit {commit} (the bundled ffmpeg/ffprobe)"),
        (f"FFmpeg-Builds-{btbn_release}.tar.gz",
         f"https://github.com/BtbN/FFmpeg-Builds/archive/refs/tags/{btbn_release}.tar.gz",
         "BtbN build scripts: how FFmpeg was configured and which library versions were linked"),
        (f"pyside-setup-everywhere-src-{qt}.tar.xz",
         f"https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{qt}-src/"
         f"pyside-setup-everywhere-src-{qt}.tar.xz", f"PySide6 and shiboken6 {qt} source"),
        (f"qtbase-everywhere-src-{qt}.tar.xz", f"{qt_sub}/qtbase-everywhere-src-{qt}.tar.xz",
         f"Qt {qt} Core, Gui, Widgets, Network and platform plugins"),
        (f"qtsvg-everywhere-src-{qt}.tar.xz", f"{qt_sub}/qtsvg-everywhere-src-{qt}.tar.xz", f"Qt {qt} Svg"),
        (f"qttranslations-everywhere-src-{qt}.tar.xz", f"{qt_sub}/qttranslations-everywhere-src-{qt}.tar.xz",
         f"Qt {qt} translations"),
    ]


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", default=str(ROOT / "dist" / "Sentinel" / "BUILD-MANIFEST.json"))
    ap.add_argument("--out", default=str(ROOT / "third_party" / "sources"))
    ap.add_argument("--btbn-release", default=BTBN_RELEASE)
    ap.add_argument("--list", action="store_true", help="only print what would be fetched")
    args = ap.parse_args(argv)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    items = source_list(manifest, args.btbn_release)
    if args.list:
        for name, url, what in items:
            print(f"{name}\n    {url}\n    {what}")
        return 0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, url, what in items:
        dest = out / name
        if not dest.exists():
            print(f"downloading {url}")
            fetch(url, dest)
        digest = sha256(dest)
        print(f"{digest}  {name}  ({dest.stat().st_size / 1e6:.1f} MB)")
        lines.append(f"{digest}  {name}\n")
    (out / "SOURCES-SHA256.txt").write_text("".join(lines), encoding="utf-8")
    (out / "SOURCES.json").write_text(json.dumps(
        {"for_build": {"version": manifest.get("version"), "git_commit": manifest.get("git_commit"),
                       "built_utc": manifest.get("built_utc")},
         "sources": [{"file": n, "url": u, "what": w} for n, u, w in items]}, indent=2) + "\n", encoding="utf-8")
    print(f"kept in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
