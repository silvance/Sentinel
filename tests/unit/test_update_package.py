"""Update files: made by packaging/release.py, refused when not genuine or not applicable, and staged
into a complete new program folder that matches the signed file list exactly."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

from sentinel.update import ed25519
from sentinel.update.apply import Folders, plan_stage, stage
from sentinel.update.package import (MANIFEST, PAYLOAD_PREFIX, SIGNATURE, UpdateRefused, check_relpath, key_id,
                                     parse_version, platform_id, read_package, write_package)

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("release_tool", ROOT / "packaging" / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)  # type: ignore[union-attr]

SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUB = ed25519.public_key(SECRET)
KID = key_id(PUB)
TRUST = {KID: PUB.hex()}

FFMPEG = b"F" * 5000                     # large, never changes
V1 = {"Sentinel.exe": b"exe 0.3.0", "SentinelService.exe": b"svc 0.3.0", "ffmpeg/ffmpeg.exe": FFMPEG,
      "_internal/qt.dll": b"qt", "_internal/old_only.pyd": b"gone in 0.3.1"}
V2 = {"Sentinel.exe": b"exe 0.3.1", "SentinelService.exe": b"svc 0.3.1", "ffmpeg/ffmpeg.exe": FFMPEG,
      "_internal/qt.dll": b"qt", "_internal/new_in_031.pyd": b"new"}


def make_build(folder: Path, version: str, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_bytes(data)
    man = {"product": "Sentinel", "version": version, "git_commit": "c" * 40, "git_dirty": False,
           "built_utc": "2026-11-10T00:00:00Z", "files": {r: hashlib.sha256(d).hexdigest() for r, d in files.items()}}
    (folder / "BUILD-MANIFEST.json").write_text(json.dumps(man), "utf-8")
    (folder / "SHA256SUMS.txt").write_text("".join(f"{s}  {r}\n" for r, s in man["files"].items()), "utf-8")
    return folder


def make_update(tmp: Path, *, old=V1, new=V2, old_v="0.3.0", new_v="0.3.1", payload=None, secret=SECRET,
                **overrides) -> Path:
    base = make_build(tmp / f"b{old_v}", old_v, old)
    nb = make_build(tmp / f"b{new_v}", new_v, new)
    files = release.target_files(nb, json.loads((nb / "BUILD-MANIFEST.json").read_text()))
    base_entry = {"version": old_v, "files": {r: hashlib.sha256((base / r).read_bytes()).hexdigest()
                                             for r in old}}
    um = {"format": "sentinel-update", "schema": 1, "product": "Sentinel", "platform": platform_id(),
          "version": new_v, "min_version": old_v, "created_utc": "x", "notes": "Fixes things.", "files": files,
          "payload": payload if payload is not None else release.payload_for(files, [base_entry])}
    um.update(overrides)
    return write_package(tmp / f"u{new_v}.sntu", um, nb, secret)


def rezip(src: Path, dest: Path, change) -> Path:   # noqa: ANN001 - change(name, data) -> data | None
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dest, "w") as zo:
        for n in zi.namelist():
            d = change(n, zi.read(n))
            if d is not None:
                zo.writestr(n, d)
    return dest


# ---------------------------------------------------------------------- versions and paths
def test_versions_order_releases_after_prereleases() -> None:
    assert parse_version("0.2.0.dev0") < parse_version("0.2.0rc1") < parse_version("0.2.0") < parse_version("0.3.0")
    assert parse_version("0.10.0") > parse_version("0.9.9")
    with pytest.raises(ValueError):
        parse_version("latest")


@pytest.mark.parametrize("rel", ["../evil.exe", "a/../../b", "/abs", "C:/x", "a\\b", "x/CON", "x/nul.txt", "a//b",
                                 "a/./b", "dir/file.", "file.exe:stream", ""])
def test_unsafe_paths_are_refused(rel: str) -> None:
    with pytest.raises(UpdateRefused):
        check_relpath(rel)


# ---------------------------------------------------------------------- making and reading
def test_update_carries_only_what_changed_and_reads_back(tmp_path: Path) -> None:
    pkg = read_package(make_update(tmp_path), installed_version="0.3.0", trusted=TRUST)
    assert pkg.payload == {"Sentinel.exe", "SentinelService.exe", "_internal/new_in_031.pyd", "BUILD-MANIFEST.json",
                           "SHA256SUMS.txt"}
    assert "ffmpeg/ffmpeg.exe" in pkg.files and "ffmpeg/ffmpeg.exe" not in pkg.payload
    assert "_internal/old_only.pyd" not in pkg.files
    assert pkg.key_id == KID and "0.3.1" in pkg.summary() and "Fixes things." in pkg.summary()


def test_payload_covers_every_release_it_can_update() -> None:
    files = {"a": {"sha256": "1"}, "b": {"sha256": "2"}, "c": {"sha256": "3"}, "BUILD-MANIFEST.json": {"sha256": "m"}}
    bases = [{"files": {"a": "1", "b": "0", "c": "3"}}, {"files": {"a": "1", "b": "2", "c": "9"}}]
    assert release.payload_for(files, bases) == ["BUILD-MANIFEST.json", "b", "c"]


def test_changed_manifest_is_refused(tmp_path: Path) -> None:
    upd = make_update(tmp_path)

    def evil(name: str, data: bytes) -> bytes:
        if name == MANIFEST:
            return data.replace(b'"0.3.1"', b'"9.9.9"')
        return data

    with pytest.raises(UpdateRefused) as e:
        read_package(rezip(upd, tmp_path / "evil.sntu", evil), installed_version="0.3.0", trusted=TRUST)
    assert e.value.code == "bad_signature"


def test_unknown_key_is_refused(tmp_path: Path) -> None:
    upd = make_update(tmp_path, secret=ed25519.new_private_key())
    with pytest.raises(UpdateRefused) as e:
        read_package(upd, installed_version="0.3.0", trusted=TRUST)
    assert e.value.code == "unknown_key"


def test_added_removed_or_resized_payload_is_refused(tmp_path: Path) -> None:
    upd = make_update(tmp_path)
    cases = {
        "extra": lambda n, d: d if n != MANIFEST else d,
        "drop": lambda n, d: None if n == PAYLOAD_PREFIX + "Sentinel.exe" else d,
        "resize": lambda n, d: d + b"x" if n == PAYLOAD_PREFIX + "Sentinel.exe" else d,
    }
    for name, change in cases.items():
        bad = rezip(upd, tmp_path / f"{name}.sntu", change)
        if name == "extra":
            with zipfile.ZipFile(bad, "a") as zf:
                zf.writestr(PAYLOAD_PREFIX + "evil.dll", b"x")
        with pytest.raises(UpdateRefused) as e:
            read_package(bad, installed_version="0.3.0", trusted=TRUST)
        assert e.value.code == "payload_mismatch", name


def test_same_size_swapped_payload_is_caught_while_staging(tmp_path: Path) -> None:
    upd = make_update(tmp_path)
    bad = rezip(upd, tmp_path / "swap.sntu",
                lambda n, d: b"EVIL 0.3.1" [:len(d)] if n == PAYLOAD_PREFIX + "Sentinel.exe" else d)
    pkg = read_package(bad, installed_version="0.3.0", trusted=TRUST)
    app = make_build(tmp_path / "Sentinel", "0.3.0", V1)
    with pytest.raises(UpdateRefused) as e:
        stage(app, pkg, Folders(app).staging("0.3.1"))
    assert e.value.code == "payload_mismatch" and not Folders(app).staging("0.3.1").exists()


def test_signed_but_unsafe_path_is_refused(tmp_path: Path) -> None:
    upd = make_update(tmp_path)
    m = json.loads(zipfile.ZipFile(upd).read(MANIFEST))
    m["files"]["../escape.exe"] = {"sha256": "0" * 64, "size": 0}
    raw = json.dumps(m).encode()
    sig = json.dumps({"key_id": KID, "ed25519": ed25519.sign(SECRET, raw).hex()}).encode()
    bad = rezip(upd, tmp_path / "path.sntu", lambda n, d: raw if n == MANIFEST else sig if n == SIGNATURE else d)
    with pytest.raises(UpdateRefused) as e:
        read_package(bad, installed_version="0.3.0", trusted=TRUST)
    assert e.value.code == "bad_path"


@pytest.mark.parametrize("installed,code", [("0.3.1", "not_newer"), ("0.4.0", "not_newer"), ("0.2.9", "too_old")])
def test_version_rules(tmp_path: Path, installed: str, code: str) -> None:
    with pytest.raises(UpdateRefused) as e:
        read_package(make_update(tmp_path), installed_version=installed, trusted=TRUST)
    assert e.value.code == code


def test_other_platform_and_product_are_refused(tmp_path: Path) -> None:
    with pytest.raises(UpdateRefused) as e:
        read_package(make_update(tmp_path, platform="linux-arm64"), installed_version="0.3.0", trusted=TRUST)
    assert e.value.code == "wrong_platform"
    with pytest.raises(UpdateRefused) as e:
        read_package(make_update(tmp_path / "p", product="Other"), installed_version="0.3.0", trusted=TRUST)
    assert e.value.code == "wrong_product"


def test_not_a_zip_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "x.sntu"
    f.write_bytes(b"MZ not a zip")
    with pytest.raises(UpdateRefused) as e:
        read_package(f, trusted=TRUST)
    assert e.value.code == "not_an_update"


# ---------------------------------------------------------------------- staging
def _tree(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_staging_builds_exactly_the_new_version_and_keeps_local_files(tmp_path: Path) -> None:
    pkg = read_package(make_update(tmp_path), installed_version="0.3.0", trusted=TRUST)
    app = make_build(tmp_path / "Sentinel", "0.3.0", V1)
    (app / "sentinel.toml").write_text("# technician's portable settings", "utf-8")
    staged = Folders(app).staging("0.3.1")
    seen = []
    plan = stage(app, pkg, staged, progress=lambda d, t, r: seen.append((d, t)))
    new = _tree(tmp_path / "b0.3.1")
    got = _tree(staged)
    assert got.pop("sentinel.toml") == b"# technician's portable settings"
    assert got == new                                       # old_only.pyd is gone, new file is there
    assert plan.local_files == ["sentinel.toml"] and seen[-1] == (plan.total, plan.total)
    assert _tree(app)["Sentinel.exe"] == b"exe 0.3.0"        # installed program untouched


def test_changed_installed_file_stops_staging(tmp_path: Path) -> None:
    pkg = read_package(make_update(tmp_path), installed_version="0.3.0", trusted=TRUST)
    app = make_build(tmp_path / "Sentinel", "0.3.0", V1)
    (app / "_internal/qt.dll").write_bytes(b"QT")               # same manifest entry, different content
    with pytest.raises(UpdateRefused) as e:
        stage(app, pkg, Folders(app).staging("0.3.1"))
    assert e.value.code == "installed_changed"


def test_missing_installed_file_means_full_install(tmp_path: Path) -> None:
    pkg = read_package(make_update(tmp_path), installed_version="0.3.0", trusted=TRUST)
    app = make_build(tmp_path / "Sentinel", "0.3.0", {k: v for k, v in V1.items() if k != "ffmpeg/ffmpeg.exe"})
    assert plan_stage(app, pkg).missing == ["ffmpeg/ffmpeg.exe"]
    with pytest.raises(UpdateRefused) as e:
        stage(app, pkg, Folders(app).staging("0.3.1"))
    assert e.value.code == "needs_files" and "full program folder" in str(e.value)


# ---------------------------------------------------------------------- release tool
def test_release_tool_records_first_release_then_makes_updates(tmp_path: Path, monkeypatch, capsys) -> None:
    rel = tmp_path / "releases"
    key = tmp_path / "k.key"
    key.write_text(SECRET.hex(), "ascii")
    monkeypatch.setattr(release, "trusted_in_source", lambda: TRUST)
    b1 = make_build(tmp_path / "d1", "0.3.0", V1)
    assert release.main(["make", "--build", str(b1), "--releases", str(rel), "--key", str(key)]) == 0
    assert "full program folder" in capsys.readouterr().out and not list(rel.glob("*/*.sntu"))
    b2 = make_build(tmp_path / "d2", "0.3.1", V2)
    assert release.main(["make", "--build", str(b2), "--releases", str(rel), "--key", str(key),
                         "--notes", "Second"]) == 0
    upd = rel / "0.3.1" / "Sentinel-update-0.3.1.sntu"
    pkg = read_package(upd, installed_version="0.3.0", trusted=TRUST)
    assert pkg.min_version == "0.3.0" and "ffmpeg/ffmpeg.exe" not in pkg.payload
    # 0.3.2 updates both 0.3.0 and 0.3.1: it carries what differs from either
    v3 = dict(V2, **{"Sentinel.exe": b"exe 0.3.2"})
    b3 = make_build(tmp_path / "d3", "0.3.2", v3)
    assert release.main(["make", "--build", str(b3), "--releases", str(rel), "--key", str(key)]) == 0
    p3 = read_package(rel / "0.3.2" / "Sentinel-update-0.3.2.sntu", installed_version="0.3.0", trusted=TRUST)
    assert {"Sentinel.exe", "SentinelService.exe", "_internal/new_in_031.pyd"} <= p3.payload
    read_package(rel / "0.3.2" / "Sentinel-update-0.3.2.sntu", installed_version="0.3.1", trusted=TRUST)


def test_release_tool_refuses_dev_versions_dirty_builds_and_untrusted_keys(tmp_path: Path, monkeypatch) -> None:
    rel, key = tmp_path / "releases", tmp_path / "k.key"
    key.write_text(SECRET.hex(), "ascii")
    monkeypatch.setattr(release, "trusted_in_source", lambda: TRUST)
    dev = make_build(tmp_path / "dev", "0.4.0.dev0", V1)
    assert release.main(["make", "--build", str(dev), "--releases", str(rel)]) == 2
    b1 = make_build(tmp_path / "d1", "0.3.0", V1)
    assert release.main(["make", "--build", str(b1), "--releases", str(rel), "--key", str(key)]) == 0
    other = tmp_path / "other.key"
    other.write_text(ed25519.new_private_key().hex(), "ascii")
    b2 = make_build(tmp_path / "d2", "0.3.1", V2)
    assert release.main(["make", "--build", str(b2), "--releases", str(rel), "--key", str(other)]) == 2


def test_keygen_writes_a_private_key_and_trusts_its_public_key(tmp_path: Path) -> None:
    keys_py = tmp_path / "keys.py"
    keys_py.write_text('TRUSTED_KEYS: dict[str, str] = {\n}\n', "utf-8")
    kid = release.add_trusted_key(PUB, keys_py)
    ns: dict = {}
    exec(keys_py.read_text("utf-8"), ns)                                            # noqa: S102
    assert ns["TRUSTED_KEYS"] == {kid: PUB.hex()}
    assert release.add_trusted_key(PUB, keys_py) == kid and keys_py.read_text().count(kid) == 1
