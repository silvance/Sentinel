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


# ---------------------------------------------------------------------- FFmpeg licence policy
# Owner decision (2026-11-10): ship an LGPL FFmpeg. GPL builds are refused unless the builder
# passes --allow-gpl-internal-use (recorded prominently); nonfree builds are always refused.
#
# Two independent sources are compared:
#  * ``ffmpeg -L`` prints the licence string that FFmpeg's configure compiled into the program;
#  * the ``configuration:`` line of ``ffmpeg -version`` (flags --enable-gpl / --enable-version3 /
#    --enable-nonfree, plus external libraries that FFmpeg only allows in GPL builds).
# Any disagreement, or an unrecognised -L text, is treated as "cannot determine" and refused.
GPL_ONLY_FLAGS = frozenset({
    "--enable-gpl", "--enable-postproc", "--enable-frei0r", "--enable-libcdio", "--enable-libdavs2",
    "--enable-librubberband", "--enable-libvidstab", "--enable-libx264", "--enable-libx265",
    "--enable-libxavs", "--enable-libxavs2", "--enable-libxvid",
})
LICENCES = ("NONFREE", "GPL-3.0-or-later", "GPL-2.0-or-later", "LGPL-3.0-or-later", "LGPL-2.1-or-later")


def licence_class(configuration: str) -> str:
    """Licence of an FFmpeg build from its configure flags (FFmpeg's LICENSE.md rules)."""
    flags = set(configuration.split())
    if "--enable-nonfree" in flags:
        return "NONFREE"
    if flags & GPL_ONLY_FLAGS:
        return "GPL-3.0-or-later" if "--enable-version3" in flags else "GPL-2.0-or-later"
    return "LGPL-3.0-or-later" if "--enable-version3" in flags else "LGPL-2.1-or-later"


def gpl_flags(configuration: str) -> list[str]:
    return sorted(set(configuration.split()) & GPL_ONLY_FLAGS)


def licence_from_text(text: str) -> str | None:
    """Licence from ``ffmpeg -L`` output (the string FFmpeg's configure compiles in)."""
    t = " ".join(text.split())
    if "nonfree parts compiled in" in t or "not legally redistributable" in t:
        return "NONFREE"
    if "GNU Lesser General Public License" in t:
        if "version 3 of the License" in t:
            return "LGPL-3.0-or-later"
        if "version 2.1 of the License" in t:
            return "LGPL-2.1-or-later"
        return None
    if "GNU General Public License" in t:
        if "version 3 of the License" in t:
            return "GPL-3.0-or-later"
        if "version 2 of the License" in t:
            return "GPL-2.0-or-later"
    return None


def licence_decision(configuration: str, licence_texts: dict[str, str], allow_gpl_internal_use: bool) -> dict:
    """Apply the policy. Returns the manifest's licence block; raises BuildError when refused."""
    from_flags = licence_class(configuration)
    from_text = {name: licence_from_text(text) for name, text in licence_texts.items()}
    unknown = [n for n, v in from_text.items() if v is None]
    if unknown:
        raise BuildError(f"cannot determine the licence of {', '.join(unknown)} from '-L'; refusing to bundle it")
    disagree = {n: v for n, v in from_text.items() if v != from_flags}
    if disagree:
        raise BuildError(f"licence evidence disagrees: configure flags say {from_flags}, '-L' says "
                         + ", ".join(f"{n}: {v}" for n, v in disagree.items()) + "; refusing to bundle it")
    block = {"license": from_flags, "license_from_flags": from_flags, "license_from_L": from_text,
             "gpl_flags": gpl_flags(configuration), "policy": "LGPL FFmpeg only (owner decision 2026-11-10)"}
    if from_flags == "NONFREE":
        raise BuildError("this FFmpeg was built with --enable-nonfree: it may not be redistributed at all; "
                         "use an LGPL build (docs/08_PACKAGING.md section 8)")
    if from_flags.startswith("GPL"):
        if not allow_gpl_internal_use:
            raise BuildError(
                f"this FFmpeg is {from_flags} (flags: {' '.join(block['gpl_flags'])}). Sentinel ships an LGPL "
                "FFmpeg only (owner decision). Use an LGPL build - see docs/08_PACKAGING.md section 8 - or, for a "
                "build that will never leave the organisation, pass --allow-gpl-internal-use (recorded in the "
                "build manifest and shown by 'Sentinel about').")
        block["license_status"] = (f"GPL FFMPEG - INTERNAL USE ONLY ({from_flags}); built with "
                                   "--allow-gpl-internal-use; must NOT be distributed")
        block["gpl_internal_use_override"] = True
    else:
        block["license_status"] = f"OK: {from_flags} (complies with the LGPL-only policy)"
        block["gpl_internal_use_override"] = False
    return block


# Components Sentinel uses (recorder, validation, content hash, MP4 export). All are FFmpeg's
# own (LGPL) code; none needs --enable-gpl.
REQUIRED_COMPONENTS = {
    "demuxers": ("rtsp", "mpegts", "mov"),
    "muxers": ("segment", "mpegts", "mp4", "framehash", "null"),
    "decoders": ("h264", "hevc"),
    "protocols": ("tcp", "udp", "file", "pipe"),
    "bsfs": ("h264_mp4toannexb", "hevc_mp4toannexb"),
}


def parse_components(kind: str, text: str) -> set[str]:
    """Names from ``ffmpeg -<kind>`` output."""
    names: set[str] = set()
    lines = text.splitlines()
    if kind in ("muxers", "demuxers", "decoders"):
        started = False
        for ln in lines:
            if ln.strip().startswith("--"):
                started = True
                continue
            parts = ln.split()
            if started and len(parts) >= 2:
                names.update(parts[1].split(","))
    elif kind == "protocols":
        section = None
        for ln in lines:
            s = ln.strip()
            if s in ("Input:", "Output:"):
                section = s
            elif section == "Input:" and s and " " not in s:
                names.add(s)
    else:  # bsfs: header line then one name per line
        names = {ln.strip() for ln in lines[1:] if ln.strip() and " " not in ln.strip()}
    return names


def missing_components(ffmpeg: Path) -> list[str]:
    missing = []
    for kind, wanted in REQUIRED_COMPONENTS.items():
        out = subprocess.run([str(ffmpeg), "-hide_banner", f"-{kind}"], capture_output=True, text=True,
                             timeout=60).stdout
        have = parse_components(kind, out)
        missing += [f"{kind[:-1]} {w}" for w in wanted if w not in have]
    return missing


def inspect_ffmpeg(folder: Path, allow_gpl_internal_use: bool = False) -> dict:
    tools = {}
    for name in ("ffmpeg", "ffprobe"):
        p = folder / f"{name}{EXE_SUFFIX}"
        if not p.is_file():
            raise BuildError(f"{p} not found (give --ffmpeg-dir the folder that contains {p.name})")
        tools[name] = p

    def run(*args: str) -> str:
        return subprocess.run([*args], capture_output=True, text=True, timeout=60, check=True).stdout

    lines = run(str(tools["ffmpeg"]), "-hide_banner", "-version").splitlines()
    version_line = next((ln for ln in lines if ln.startswith("ffmpeg version")), lines[0] if lines else "")
    configuration = next((ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("configuration:")), "")
    probe_line = run(str(tools["ffprobe"]), "-hide_banner", "-version").splitlines()[0]
    texts = {n: run(str(p), "-hide_banner", "-L") for n, p in tools.items()}
    lic = licence_decision(configuration, texts, allow_gpl_internal_use)
    missing = missing_components(tools["ffmpeg"])
    if missing:
        raise BuildError("this FFmpeg lacks components Sentinel needs: " + ", ".join(missing))
    # Licence/readme texts shipped with the FFmpeg build (gyan.dev: next to bin/; others: same folder).
    search = (folder, folder.parent) if folder.name.lower() == "bin" else (folder,)
    notices = [p for d in search for p in sorted(d.glob("*"))
               if p.is_file() and p.name.upper().startswith(("LICENSE", "COPYING", "README"))]
    return {"tools": tools, "version": version_line.removeprefix("ffmpeg version ").split(" Copyright")[0],
            "ffprobe_version": probe_line.removeprefix("ffprobe version ").split(" Copyright")[0],
            "configuration": configuration, "license": lic["license"], "licence_block": lic, "notices": notices}

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
Licence:        {license}   (from "ffmpeg -L", cross-checked with the configure flags)
Status:         {status}

Sentinel's policy (owner decision 2026-11-10) is to ship an LGPL FFmpeg. An LGPL build must be
accompanied by its licence text (in this folder) and its source must be obtainable. A GPL build
is only present if the builder explicitly chose --allow-gpl-internal-use: such a folder must NOT
be given to anyone outside the organisation. See docs/08_PACKAGING.md section 8.
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


def build(ffmpeg_dir: Path, dist_root: Path, work: Path, allow_dirty: bool,
          allow_gpl_internal_use: bool = False) -> Path:
    commit = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    if dirty and not allow_dirty:
        raise BuildError("the source tree has uncommitted changes; commit them or pass --allow-dirty "
                         "(the manifest then says so)")
    ff = inspect_ffmpeg(ffmpeg_dir, allow_gpl_internal_use)
    print(f"FFmpeg {ff['version']} from {ffmpeg_dir}: {ff['licence_block']['license_status']}")
    app = dist_root / "Sentinel"
    started = time.time()
    run_pyinstaller(dist_root, work)
    if not (app / f"{'Sentinel' if IS_WIN else 'sentinel'}{EXE_SUFFIX}").is_file():
        raise BuildError(f"PyInstaller did not produce the program in {app}")
    if IS_WIN and not (app / f"{spec_common.SERVICE_EXE_NAME}{EXE_SUFFIX}").is_file():
        raise BuildError(f"PyInstaller did not produce the service host {spec_common.SERVICE_EXE_NAME}{EXE_SUFFIX}")

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
        LICENCE_NOTE.format(exe=EXE_SUFFIX, version=ff["version"], license=ff["license"],
                            status=ff["licence_block"]["license_status"]), encoding="utf-8")
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
        # Shown first and by "Sentinel about": the licence status of the bundled FFmpeg.
        "ffmpeg_license_status": ff["licence_block"]["license_status"],
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
                   "configuration": ff["configuration"], **ff["licence_block"],
                   "required_components": {k: list(v) for k, v in REQUIRED_COMPONENTS.items()},
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
    ap.add_argument("--allow-gpl-internal-use", action="store_true",
                    help="bundle a GPL FFmpeg anyway, for a build that never leaves the organisation "
                         "(recorded in BUILD-MANIFEST.json and shown by 'Sentinel about'); nonfree is never allowed")
    ap.add_argument("--inspect-only", action="store_true",
                    help="only check the FFmpeg folder (licence, required components) and print the result")
    args = ap.parse_args(argv)
    try:
        if args.inspect_only:
            ff = inspect_ffmpeg(args.ffmpeg_dir.resolve(), args.allow_gpl_internal_use)
            print(json.dumps({"version": ff["version"], **ff["licence_block"],
                              "required_components": "all present"}, indent=2))
            return 0
        build(args.ffmpeg_dir.resolve(), args.dist.resolve(), args.work.resolve(), args.allow_dirty,
              args.allow_gpl_internal_use)
    except (BuildError, subprocess.CalledProcessError) as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
