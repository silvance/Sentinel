"""Make Sentinel releases and their update files (owner request 2026-11-10).

    python packaging/release.py keygen                # once: the release signing key
    python packaging/release.py make [--notes "..."]  # after each build of a new version
    python packaging/release.py list

``keygen`` creates the Ed25519 private key outside the repository
(``%USERPROFILE%\\.sentinel-release\\update-signing.key``, readable by this account only) and adds its
public key to ``src/sentinel/update/keys.py``; builds made after that trust it. **Back the private key
up** (offline, e.g. on the release USB stick in a safe): without it no further update file can be
made, and computers would need the full program folder again.

``make`` records the build in ``dist/Sentinel`` as a release in ``releases/<version>/`` (git-ignored;
keep it with ``third_party/sources``) and writes ``releases/<version>/Sentinel-update-<version>.sntu``.
The update file can update every recorded release from ``--min-version`` (default: the oldest
recorded one) up: it carries each file that differs from at least one of them. The very first release
has nothing to update; it is installed as a full program folder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sentinel.update import ed25519  # noqa: E402
from sentinel.update.package import (  # noqa: E402
    BUILD_FILES,
    EXTENSION,
    FORMAT,
    PRODUCT,
    SCHEMA,
    is_release,
    key_id,
    parse_version,
    platform_id,
    read_package,
    write_package,
)

DEFAULT_KEY = Path.home() / ".sentinel-release" / "update-signing.key"
KEYS_PY = ROOT / "src" / "sentinel" / "update" / "keys.py"
RELEASE_JSON = "release.json"


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def trusted_in_source() -> dict[str, str]:
    ns: dict = {}
    exec(KEYS_PY.read_text("utf-8"), ns)   # noqa: S102 - our own file
    return dict(ns["TRUSTED_KEYS"])


def load_key(path: Path) -> bytes:
    key = bytes.fromhex(path.read_text("ascii").strip())
    if len(key) != 32:
        raise SystemExit(f"{path} is not an Ed25519 private key")
    return key


# ---------------------------------------------------------------------- keygen
def _private_file(path: Path) -> None:
    if sys.platform == "win32":
        user = os.environ.get("USERNAME", "")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"], check=True,
                       capture_output=True)
    else:
        path.chmod(0o600)


def add_trusted_key(pub: bytes, keys_py: Path = KEYS_PY) -> str:
    kid = key_id(pub)
    text = keys_py.read_text("utf-8")
    if kid in text:
        return kid
    marker = "TRUSTED_KEYS: dict[str, str] = {\n"
    if marker not in text:
        raise SystemExit(f"cannot find TRUSTED_KEYS in {keys_py}")
    entry = f'    "{kid}": "{pub.hex()}",   # added {time.strftime("%Y-%m-%d")}\n'
    keys_py.write_bytes(text.replace(marker, marker + entry).encode("utf-8"))
    return kid


def cmd_keygen(args: argparse.Namespace) -> int:
    path = Path(args.key)
    if path.exists():
        print(f"{path} already exists; not replacing a release key. (Delete it yourself only if it was never "
              "used, or follow the key change in src/sentinel/update/keys.py.)")
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = ed25519.new_private_key()
    path.write_text(secret.hex() + "\n", "ascii")
    _private_file(path)
    pub = ed25519.public_key(secret)
    kid = add_trusted_key(pub)
    print(f"Release signing key {kid} written to {path} (this account only).")
    print(f"Its public key was added to {KEYS_PY.relative_to(ROOT)}; commit that before the next build.")
    print("BACK UP THE PRIVATE KEY FILE NOW (offline). Without it, no further update file can be made.")
    return 0


# ---------------------------------------------------------------------- releases
def recorded(releases: Path) -> list[dict]:
    out = []
    for f in releases.glob(f"*/{RELEASE_JSON}"):
        try:
            out.append(json.loads(f.read_text("utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(out, key=lambda r: parse_version(r["version"]))


def target_files(build: Path, manifest: dict) -> dict[str, dict]:
    files = {}
    for rel, sha in manifest["files"].items():
        files[rel] = {"sha256": sha, "size": (build / rel).stat().st_size}
    for rel in BUILD_FILES:
        p = build / rel
        if p.is_file():
            files[rel] = {"sha256": _sha(p), "size": p.stat().st_size}
    return files


def payload_for(files: dict[str, dict], bases: list[dict]) -> list[str]:
    """Files that differ from at least one base release (and the two build files, always)."""
    return sorted(rel for rel, meta in files.items()
                  if rel in BUILD_FILES or any(b["files"].get(rel) != meta["sha256"] for b in bases))


def cmd_make(args: argparse.Namespace) -> int:
    build, releases = Path(args.build), Path(args.releases)
    try:
        manifest = json.loads((build / "BUILD-MANIFEST.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"No build in {build}: {exc}")
        return 2
    version = str(manifest["version"])
    if not is_release(version) and not args.allow_prerelease:
        print(f"{version} is not a release version (X.Y.Z). Set __version__ in src/sentinel/__init__.py and build again.")
        return 2
    if manifest.get("git_dirty") and not args.allow_dirty:
        print("The build was made from uncommitted changes; releases must be built from a commit.")
        return 2
    files = target_files(build, manifest)
    trusted = trusted_in_source()
    entry = {"version": version, "git_commit": manifest.get("git_commit"), "built_utc": manifest.get("built_utc"),
             "platform": platform_id(), "trusted_key_ids": sorted(trusted),
             "files": {rel: m["sha256"] for rel, m in files.items()}}
    earlier = [r for r in recorded(releases) if parse_version(r["version"]) < parse_version(version)]
    same = [r for r in recorded(releases) if r["version"] == version]
    if same and same[0]["files"] != entry["files"] and not args.replace:
        print(f"Release {version} is already recorded with different files. Raise the version number "
              "(or use --replace if that release was never handed out).")
        return 2
    out_dir = releases / version
    out_dir.mkdir(parents=True, exist_ok=True)
    min_version = args.min_version or (earlier[0]["version"] if earlier else None)
    bases = [r for r in earlier if min_version and parse_version(r["version"]) >= parse_version(min_version)]
    if not bases:
        (out_dir / RELEASE_JSON).write_text(json.dumps(entry, indent=1), "utf-8")
        print(f"Recorded release {version} ({len(files)} files). There is no earlier release to update from: "
              "install this version as the full program folder. Later versions can update from it.")
        return 0
    key_path = Path(args.key)
    if not key_path.is_file():
        print(f"No release signing key at {key_path} (run 'keygen' once, or pass --key).")
        return 2
    secret = load_key(key_path)
    kid = key_id(ed25519.public_key(secret))
    untrusting = [b["version"] for b in bases if kid not in b.get("trusted_key_ids", [])]
    if untrusting:
        print(f"Release key {kid} is not trusted by {', '.join(untrusting)}; those versions would refuse the "
              "update. Use the key they trust, or raise --min-version.")
        return 2
    payload = payload_for(files, bases)
    notes = Path(args.notes_file).read_text("utf-8") if args.notes_file else (args.notes or "")
    um = {"format": FORMAT, "schema": SCHEMA, "product": PRODUCT, "platform": platform_id(), "version": version,
          "min_version": min_version, "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "git_commit": manifest.get("git_commit"), "notes": notes.strip(), "files": files, "payload": payload}
    out = out_dir / f"Sentinel-update-{version}{EXTENSION}"
    write_package(out, um, build, secret)
    pkg = read_package(out, installed_version=min_version, trusted=trusted)   # read back as a computer would
    (out_dir / RELEASE_JSON).write_text(json.dumps(entry, indent=1), "utf-8")
    size = out.stat().st_size
    print(f"Update file: {out}")
    print(f"  Sentinel {version}, updates {min_version} or later ({', '.join(b['version'] for b in bases)})")
    print(f"  {len(payload)} of {len(files)} files carried, {size / 1e6:.1f} MB; signed by {kid}")
    print(f"  SHA-256 {pkg.sha256}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rel = recorded(Path(args.releases))
    if not rel:
        print("No releases recorded.")
    for r in rel:
        upd = list((Path(args.releases) / r["version"]).glob(f"*{EXTENSION}"))
        print(f"{r['version']:10s} {r.get('built_utc')}  {str(r.get('git_commit'))[:10]}  "
              f"{upd[0].name if upd else '(full install only)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="release.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("keygen", help="create the release signing key (once)")
    p.add_argument("--key", default=str(DEFAULT_KEY))
    p = sub.add_parser("make", help="record the build as a release and write its update file")
    p.add_argument("--build", default=str(ROOT / "dist" / "Sentinel"))
    p.add_argument("--releases", default=str(ROOT / "releases"))
    p.add_argument("--key", default=str(DEFAULT_KEY))
    p.add_argument("--min-version", help="oldest installed version the update file must update (default: oldest recorded)")
    p.add_argument("--notes", help="release notes shown before applying")
    p.add_argument("--notes-file")
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--allow-prerelease", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--replace", action="store_true", help="re-record a version that was never handed out")
    p = sub.add_parser("list", help="recorded releases")
    p.add_argument("--releases", default=str(ROOT / "releases"))
    args = ap.parse_args(argv)
    return {"keygen": cmd_keygen, "make": cmd_make, "list": cmd_list}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
