"""Build the click-to-run Sentinel folder (Windows or Linux) and its build manifest.

    <build-venv-python> packaging/build.py --ffmpeg-dir <folder containing ffmpeg[.exe] and ffprobe[.exe]>

Steps (docs/08_PACKAGING.md):
1. Record the source state (git commit, uncommitted changes) and the build environment.
2. Inspect the FFmpeg given on the command line: version, configure flags, licence class.
   It is never downloaded; a build with ``--enable-nonfree`` is refused (not redistributable).
3. Run PyInstaller with the platform's spec (one-folder) with PYTHONHASHSEED=0 and
   SOURCE_DATE_EPOCH = commit time, into ``dist/Sentinel``.
4. Copy FFmpeg/FFprobe and their licence files into ``dist/Sentinel/ffmpeg/`` unchanged.
5. Write ``BUILD-MANIFEST.json`` (versions, FFmpeg facts, SHA-256 of every file) and
   ``SHA256SUMS.txt``; ``Sentinel about --verify`` checks an installed copy against it.

Run it with the interpreter of the build virtualenv (requirements/build.txt); the wrappers
packaging/windows/build.ps1 and packaging/linux/build.sh create that virtualenv.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as pyplatform
import shutil
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import spec_common  # noqa: E402

IS_WIN = sys.platform == "win32"
EXE_SUFFIX = ".exe" if IS_WIN else ""
SPEC = ROOT / "packaging" / ("windows" if IS_WIN else "linux") / "sentinel.spec"
MANIFEST_NAME = "BUILD-MANIFEST.json"     # same name as sentinel.paths.MANIFEST_NAME
SUMS_NAME = "SHA256SUMS.txt"


class BuildError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True,
                              timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def licence_class(configuration: str) -> str:
    """Licence of an FFmpeg build from its configure flags (FFmpeg's own LICENSE.md rules)."""
    flags = configuration.split()
    if "--enable-nonfree" in flags:
        return "NONFREE (not redistributable)"
    if "--enable-gpl" in flags:
        return "GPL-3.0-or-later" if "--enable-version3" in flags else "GPL-2.0-or-later"
    return "LGPL-3.0-or-later" if "--enable-version3" in flags else "LGPL-2.1-or-later"


def inspect_ffmpeg(folder: Path) -> dict:
    tools = {}
    for name in ("ffmpeg", "ffprobe"):
        p = folder / f"{name}{EXE_SUFFIX}"
        if not p.is_file():
            raise BuildError(f"{p} not found (give --ffmpeg-dir the folder that contains {p.name})")
        tools[name] = p
    out = subprocess.run([str(tools["ffmpeg"]), "-hide_banner", "-version"], capture_output=True, text=True,
                         timeout=60, check=True).stdout
    lines = out.splitlines()
    version_line = next((ln for ln in lines if ln.startswith("ffmpeg version")), lines[0] if lines else "")
    configuration = next((ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("configuration:")), "")
    probe_line = subprocess.run([str(tools["ffprobe"]), "-hide_banner", "-version"], capture_output=True,
                                text=True, timeout=60, check=True).stdout.splitlines()[0]
    lic = licence_class(configuration)
    if lic.startswith("NONFREE"):
        raise BuildError("this FFmpeg was built with --enable-nonfree and may not be redistributed")
    # Licence/readme texts shipped with the FFmpeg build (gyan.dev: next to bin/; others: same folder).
    search = (folder, folder.parent) if folder.name.lower() == "bin" else (folder,)
    notices = [p for d in search for p in sorted(d.glob("*"))
               if p.is_file() and p.name.upper().startswith(("LICENSE", "COPYING", "README"))]
    return {"tools": tools, "version": version_line.removeprefix("ffmpeg version ").split(" Copyright")[0],
            "ffprobe_version": probe_line.removeprefix("ffprobe version ").split(" Copyright")[0],
            "configuration": configuration, "license": lic, "notices": notices}


def run_pyinstaller(dist: Path, work: Path) -> None:
    env = dict(os.environ, PYTHONHASHSEED="0")
    epoch = git("log", "-1", "--format=%ct")
    if epoch:
        env["SOURCE_DATE_EPOCH"] = epoch
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
           "--distpath", str(dist), "--workpath", str(work), str(SPEC)]
    print("running:", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


LICENCE_NOTE = """\
FFmpeg in this folder
=====================
ffmpeg{exe} and ffprobe{exe} are unmodified copies of the FFmpeg build named below. They are
separate programs that Sentinel runs; Sentinel does not link to them.

Version:        {version}
Licence class:  {license}   (derived from the configure flags in BUILD-MANIFEST.json)

If the licence class is GPL, anyone who distributes this folder must also make the complete
corresponding source code of this exact FFmpeg build (and of the libraries compiled into it)
available under the GPL, and pass on the licence text (LICENSE file in this folder). See
docs/08_PACKAGING.md section "FFmpeg licensing" for the options; the distribution decision is
the project owner's.
"""


def write_tree_manifest(app: Path, meta: dict) -> dict:
    files = {}
    for p in sorted(app.rglob("*")):
        if p.is_file() and p.name not in (MANIFEST_NAME, SUMS_NAME):
            files[p.relative_to(app).as_posix()] = sha256(p)
    meta["files"] = files
    meta["file_count"] = len(files)
    meta["total_bytes"] = sum((app / r).stat().st_size for r in files)
    (app / MANIFEST_NAME).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    (app / SUMS_NAME).write_text("".join(f"{h}  {r}\n" for r, h in files.items()), encoding="utf-8")
    return meta


def build(ffmpeg_dir: Path, dist_root: Path, work: Path, allow_dirty: bool) -> Path:
    commit = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    if dirty and not allow_dirty:
        raise BuildError("the source tree has uncommitted changes; commit them or pass --allow-dirty "
                         "(the manifest then says so)")
    ff = inspect_ffmpeg(ffmpeg_dir)
    print(f"FFmpeg {ff['version']} ({ff['license']}) from {ffmpeg_dir}")
    app = dist_root / "Sentinel"
    started = time.time()
    run_pyinstaller(dist_root, work)
    if not (app / f"{'Sentinel' if IS_WIN else 'sentinel'}{EXE_SUFFIX}").is_file():
        raise BuildError(f"PyInstaller did not produce the program in {app}")

    tools_dir = app / "ffmpeg"
    tools_dir.mkdir(exist_ok=True)
    tool_meta = {}
    for name, src in ff["tools"].items():
        dst = tools_dir / src.name
        shutil.copy2(src, dst)
        if sha256(dst) != sha256(src):
            raise BuildError(f"copy of {src} does not match the original")
        tool_meta[src.name] = {"sha256": sha256(dst), "size_bytes": dst.stat().st_size, "source": str(src)}
    for notice in ff["notices"]:
        shutil.copy2(notice, tools_dir / (notice.name if notice.suffix else notice.name + ".txt"))
    (tools_dir / "FFMPEG-LICENCE-NOTE.txt").write_text(
        LICENCE_NOTE.format(exe=EXE_SUFFIX, version=ff["version"], license=ff["license"]), encoding="utf-8")
    readme = ROOT / "packaging" / ("windows" if IS_WIN else "linux") / "READ-ME-FIRST.txt"
    shutil.copy2(readme, app / readme.name)
    if not IS_WIN:
        shutil.copy2(ROOT / "packaging" / "linux" / "sentinel.desktop", app / "sentinel.desktop")
        installer = app / "install-desktop-entry.sh"
        shutil.copy2(ROOT / "packaging" / "linux" / "install-desktop-entry.sh", installer)
        installer.chmod(0o755)
        for name in ("ffmpeg", "ffprobe"):
            (tools_dir / name).chmod(0o755)

    try:
        pyi_version = metadata.version("pyinstaller")
    except metadata.PackageNotFoundError:
        pyi_version = "unknown"
    meta = {
        "product": "Sentinel",
        "version": spec_common.version(),
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_commit": commit or "unknown",
        "git_dirty": dirty,
        "build_host": {"os": pyplatform.platform(), "machine": pyplatform.machine()},
        "python": sys.version.split()[0],
        "pyinstaller": pyi_version,
        "build_environment": sorted(f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions()),
        "pyinstaller_settings": {"mode": "onedir", "upx": False, "console": True, "PYTHONHASHSEED": "0",
                                 "SOURCE_DATE_EPOCH": git("log", "-1", "--format=%ct") or None},
        "ffmpeg": {"version": ff["version"], "ffprobe_version": ff["ffprobe_version"],
                   "configuration": ff["configuration"], "license": ff["license"],
                   "source_dir": str(ffmpeg_dir), "files": tool_meta},
    }
    meta = write_tree_manifest(app, meta)
    print(f"built {app} in {time.time() - started:.0f} s: {meta['file_count']} files, "
          f"{meta['total_bytes'] / 1e6:.1f} MB ({sum(t['size_bytes'] for t in tool_meta.values()) / 1e6:.1f} MB FFmpeg)")
    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ffmpeg-dir", required=True, type=Path,
                    help="folder that contains the ffmpeg and ffprobe executables to bundle")
    ap.add_argument("--dist", type=Path, default=ROOT / "dist", help="output root (default: dist/)")
    ap.add_argument("--work", type=Path, default=ROOT / "build" / f"pyinstaller-{sys.platform}")
    ap.add_argument("--allow-dirty", action="store_true", help="build from uncommitted changes (recorded)")
    args = ap.parse_args(argv)
    try:
        build(args.ffmpeg_dir.resolve(), args.dist.resolve(), args.work.resolve(), args.allow_dirty)
    except (BuildError, subprocess.CalledProcessError) as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
