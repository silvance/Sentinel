"""Update files (``Sentinel-update-<version>.sntu``): one file that moves an installed Sentinel to a
newer version (owner request 2026-11-10: no reinstalling the whole program for every change).

An update file is a zip archive:

* ``update.json``  - what the update is: product, platform, ``version``, ``min_version`` (the oldest
  installed version it can update), and ``files``: SHA-256 and size of *every* file of the new
  version's program folder. ``payload`` names the files carried in the archive: those that differ
  from at least one release it can update (usually ``Sentinel.exe``, ``SentinelService.exe`` and the
  two manifests; FFmpeg and the Qt libraries rarely change).
* ``update.sig``   - an Ed25519 signature of the exact bytes of ``update.json`` by a release key
  (``keys.TRUSTED_KEYS``). The signature covers every file through its SHA-256.
* ``files/<path>`` - the payload.

Nothing in the archive is trusted before the signature is checked, and no payload file is used
unless its SHA-256 matches ``update.json``. Paths are checked so nothing can be written outside the
program folder. Made by ``packaging/release.py``.
"""

from __future__ import annotations

import hashlib
import json
import platform as _platform
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import ed25519
from .keys import TRUSTED_KEYS

FORMAT = "sentinel-update"
SCHEMA = 1
EXTENSION = ".sntu"
MANIFEST = "update.json"
SIGNATURE = "update.sig"
PAYLOAD_PREFIX = "files/"
PRODUCT = "Sentinel"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
# The two files every build rewrites; always carried in the payload.
BUILD_FILES = ("BUILD-MANIFEST.json", "SHA256SUMS.txt")

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.?(dev|a|b|rc)(\d+))?$")
_PRE_RANK = {"dev": 0, "a": 1, "b": 2, "rc": 3}
_WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.I)


class UpdateRefused(Exception):
    """The update file cannot be used. ``code`` is a machine token; the message is for people."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------- versions
def parse_version(text: str) -> tuple[int, int, int, int, int]:
    """'0.3.0' -> (0, 3, 0, 9, 0); '0.2.0.dev0' -> (0, 2, 0, 0, 0). Pre-releases sort before the release."""
    m = _VERSION.match(str(text).strip())
    if not m:
        raise ValueError(f"not a version number: {text!r}")
    rank = _PRE_RANK[m.group(4)] if m.group(4) else 9
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), rank, int(m.group(5) or 0)


def is_release(text: str) -> bool:
    m = _VERSION.match(str(text).strip())
    return bool(m) and m.group(4) is None


def platform_id(plat: str | None = None, machine: str | None = None) -> str:
    """'windows-x64' / 'linux-x64' / 'linux-arm64' (an update only applies to the same platform)."""
    p = plat or sys.platform
    m = (machine or _platform.machine()).lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m)
    return f"{'windows' if p == 'win32' else p}-{arch}"


def key_id(public: bytes) -> str:
    return hashlib.sha256(public).hexdigest()[:16]


# ---------------------------------------------------------------------- paths
def check_relpath(rel: str) -> str:
    """A program-folder path from update.json: relative, '/'-separated, no '..', no drive, no
    Windows device names, no alternate data streams. Raises UpdateRefused otherwise."""
    bad = UpdateRefused("bad_path", f"The update file names an unsafe path ({rel!r}). Nothing was changed.")
    if not isinstance(rel, str) or not rel or "\\" in rel or ":" in rel or "\x00" in rel or rel.startswith("/"):
        raise bad
    parts = PurePosixPath(rel).parts
    if any(p in ("", ".", "..") or p.endswith((" ", ".")) or _WIN_RESERVED.match(p) for p in parts):
        raise bad
    if PurePosixPath(rel).as_posix() != rel:
        raise bad
    return rel


# ---------------------------------------------------------------------- reading
@dataclass
class Package:
    path: Path
    manifest: dict[str, Any]
    raw_manifest: bytes
    key_id: str
    sha256: str                              # of the whole update file (recorded in the recording log)
    payload: set[str] = field(default_factory=set)

    @property
    def version(self) -> str:
        return str(self.manifest["version"])

    @property
    def min_version(self) -> str:
        return str(self.manifest["min_version"])

    @property
    def files(self) -> dict[str, dict[str, Any]]:
        return self.manifest["files"]

    def summary(self) -> str:
        notes = str(self.manifest.get("notes") or "").strip()
        size = sum(int(self.files[r]["size"]) for r in self.payload)
        return (f"Sentinel {self.version} (updates {self.min_version} or later); {len(self.payload)} of "
                f"{len(self.files)} files carried, {size / 1e6:.1f} MB; signed by release key {self.key_id}."
                + (f"\n\n{notes}" if notes else ""))

    def open_payload(self, rel: str):  # noqa: ANN201 - zipfile.ZipExtFile
        if rel not in self.payload:
            raise KeyError(rel)
        return zipfile.ZipFile(self.path).open(PAYLOAD_PREFIX + rel)


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_package(path: Path, *, installed_version: str | None = None, trusted: dict[str, str] | None = None,
                 plat: str | None = None, allow_same: bool = False) -> Package:
    """Open and check an update file. Raises UpdateRefused (nothing changed) when it is not a
    genuine Sentinel update for this platform, newer than ``installed_version`` and applicable to it."""
    trusted = TRUSTED_KEYS if trusted is None else trusted
    path = Path(path)
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateRefused("not_an_update", f"This is not a Sentinel update file ({exc}).") from exc
    with zf:
        names = set(zf.namelist())
        if MANIFEST not in names or SIGNATURE not in names:
            raise UpdateRefused("not_an_update", "This is not a Sentinel update file (no update.json/update.sig).")
        if zf.getinfo(MANIFEST).file_size > MAX_MANIFEST_BYTES:
            raise UpdateRefused("not_an_update", "This is not a Sentinel update file (update.json too large).")
        raw = zf.read(MANIFEST)
        try:
            sig = json.loads(zf.read(SIGNATURE).decode("utf-8"))
            kid, signature = str(sig["key_id"]), bytes.fromhex(str(sig["ed25519"]))
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            raise UpdateRefused("bad_signature", "The update file's signature cannot be read. "
                                "Nothing was changed.") from exc
        pub_hex = trusted.get(kid)
        if pub_hex is None:
            raise UpdateRefused("unknown_key", f"The update file is signed with a key this computer does not "
                                f"trust ({kid}). Nothing was changed.")
        if key_id(bytes.fromhex(pub_hex)) != kid or not ed25519.verify(bytes.fromhex(pub_hex), raw, signature):
            raise UpdateRefused("bad_signature", "The update file is damaged or has been changed since it was "
                                "signed. Nothing was changed.")
        # Signed: from here on update.json is trusted, the payload only through its hashes.
        try:
            m = json.loads(raw.decode("utf-8"))
            ok = (m["format"] == FORMAT and int(m["schema"]) == SCHEMA and isinstance(m["files"], dict)
                  and isinstance(m["payload"], list))
            parse_version(m["version"])
            parse_version(m["min_version"])
        except (ValueError, KeyError, TypeError) as exc:
            raise UpdateRefused("not_an_update", f"The update file is not in a format this version understands "
                                f"({exc}).") from exc
        if not ok:
            raise UpdateRefused("not_an_update", "The update file is not in a format this version understands.")
        if m.get("product") != PRODUCT:
            raise UpdateRefused("wrong_product", "This update file is not for Sentinel.")
        here = platform_id(plat)
        if m.get("platform") != here:
            raise UpdateRefused("wrong_platform", f"This update file is for {m.get('platform')}; this computer "
                                f"is {here}.")
        for rel, meta in m["files"].items():
            check_relpath(rel)
            if not (isinstance(meta, dict) and re.fullmatch(r"[0-9a-f]{64}", str(meta.get("sha256", "")))
                    and isinstance(meta.get("size"), int) and meta["size"] >= 0):
                raise UpdateRefused("not_an_update", f"The update file lists {rel} without a valid hash.")
        payload = set(m["payload"])
        if not payload <= set(m["files"]):
            raise UpdateRefused("not_an_update", "The update file carries files that are not in its file list.")
        carried = {n[len(PAYLOAD_PREFIX):] for n in names if n.startswith(PAYLOAD_PREFIX) and not n.endswith("/")}
        if carried != payload:
            raise UpdateRefused("payload_mismatch", "The update file's contents do not match its file list. "
                                "Nothing was changed.")
        for rel in sorted(payload):
            info = zf.getinfo(PAYLOAD_PREFIX + rel)
            if info.file_size != m["files"][rel]["size"]:
                raise UpdateRefused("payload_mismatch", f"{rel} in the update file has the wrong size.")
    if installed_version is not None:
        new, cur = parse_version(m["version"]), parse_version(installed_version)
        if new < cur or (new == cur and not allow_same):
            raise UpdateRefused("not_newer", f"This update ({m['version']}) is not newer than the installed "
                                f"version ({installed_version}). Nothing was changed.")
        if cur < parse_version(m["min_version"]):
            raise UpdateRefused("too_old", f"This update needs Sentinel {m['min_version']} or later; this computer "
                                f"has {installed_version}. Install the full program folder instead.")
    return Package(path, m, raw, kid, _sha256_of(path), payload)


# ---------------------------------------------------------------------- writing (packaging/release.py)
def write_package(out: Path, manifest: dict[str, Any], payload_root: Path, private_key: bytes) -> Path:
    """Write and sign an update file. ``payload_root`` holds the new program folder; the files named
    in ``manifest['payload']`` are taken from it and checked against ``manifest['files']``."""
    for rel in manifest["payload"]:
        check_relpath(rel)
        actual = _sha256_of(payload_root / rel)
        if actual != manifest["files"][rel]["sha256"]:
            raise ValueError(f"{rel} changed while the update file was being made")
    raw = json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8")
    pub = ed25519.public_key(private_key)
    sig = {"key_id": key_id(pub), "ed25519": ed25519.sign(private_key, raw).hex()}
    tmp = out.with_name(out.name + ".partial")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr(MANIFEST, raw)
        zf.writestr(SIGNATURE, json.dumps(sig, indent=1))
        for rel in sorted(manifest["payload"]):
            zf.write(payload_root / rel, PAYLOAD_PREFIX + rel)
    tmp.replace(out)
    return out
