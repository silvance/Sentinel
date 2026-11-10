"""DPAPI-protected camera credentials (real CryptProtectData/CryptUnprotectData on Windows)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sentinel.credentials import DPAPI_MAGIC, CredentialError, credentials_toml, load_credentials

win = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
AWKWARD_PASSWORD = 'p@ss "quoted" \\ back\\slash\ttab ü€ 🙂 = [x] # not a comment'


def test_credentials_toml_round_trips_awkward_values(tmp_path: Path) -> None:
    f = tmp_path / "camera.secret"
    f.write_text(credentials_toml("view er", AWKWARD_PASSWORD), encoding="utf-8")
    c = load_credentials(f)
    assert (c.username, c.password, c.protection) == ("view er", AWKWARD_PASSWORD, "plaintext")
    assert "p@ss" not in repr(c)


@win
@pytest.mark.parametrize("scope", ["user", "machine"])
def test_dpapi_round_trip(scope: str) -> None:
    from sentinel.winsvc import dpapi

    blob = dpapi.protect(b"secret bytes", scope=scope)  # type: ignore[arg-type]
    assert b"secret bytes" not in blob
    assert dpapi.unprotect(blob) == b"secret bytes"


@win
def test_dpapi_wrong_entropy_or_damaged_blob_fails() -> None:
    from sentinel.winsvc import dpapi

    blob = dpapi.protect(b"x", scope="machine")
    with pytest.raises(dpapi.DpapiError):
        dpapi.unprotect(blob, entropy=b"another application")
    with pytest.raises(dpapi.DpapiError):
        dpapi.unprotect(blob[:-8] + bytes(8))


@win
@pytest.mark.parametrize("scope", ["user", "machine"])
def test_protected_credentials_file_round_trip(tmp_path: Path, scope: str) -> None:
    from sentinel.winsvc.credstore import write_protected_credentials

    f = tmp_path / "camera.dpapi"
    write_protected_credentials(f, "viewer", AWKWARD_PASSWORD, scope=scope, acl_argv=None)  # type: ignore[arg-type]
    raw = f.read_bytes()
    assert raw.startswith(DPAPI_MAGIC + f" scope={scope}".encode())
    assert AWKWARD_PASSWORD.encode() not in raw and b"viewer" not in raw
    c = load_credentials(f)
    assert (c.username, c.password, c.protection) == ("viewer", AWKWARD_PASSWORD, "dpapi")
    assert not list(tmp_path.glob(".*.tmp"))


@win
def test_protected_file_restricts_acl_before_writing_the_secret(tmp_path: Path) -> None:
    from sentinel.winsvc.credstore import write_protected_credentials

    seen: list[tuple[list[str], int]] = []

    def run(argv):  # noqa: ANN001, ANN202
        import subprocess
        tmp = Path(argv[1])
        seen.append((list(argv), tmp.stat().st_size))          # the file must still be empty here
        return subprocess.CompletedProcess(argv, 0, "", "")

    f = tmp_path / "camera.dpapi"
    write_protected_credentials(f, "viewer", "pw", acl_argv=lambda p: ["icacls", str(p), "/inheritance:r"], run=run)
    assert len(seen) == 1 and seen[0][1] == 0 and seen[0][0][1] != str(f)
    assert load_credentials(f).protection == "dpapi"


@win
def test_failed_acl_aborts_without_writing(tmp_path: Path) -> None:
    import subprocess

    from sentinel.winsvc.credstore import write_protected_credentials

    f = tmp_path / "camera.dpapi"
    with pytest.raises(CredentialError):
        write_protected_credentials(f, "viewer", "pw", acl_argv=lambda p: ["icacls", str(p)],
                                    run=lambda argv: subprocess.CompletedProcess(argv, 5, "Access is denied.", ""))
    assert not f.exists() and not list(tmp_path.glob(".*"))


@win
def test_damaged_protected_file_is_a_credential_error(tmp_path: Path) -> None:
    f = tmp_path / "camera.dpapi"
    f.write_bytes(DPAPI_MAGIC + b" scope=machine\n" + b"AAAA" * 20 + b"\n")
    with pytest.raises(CredentialError, match="cannot be decrypted"):
        load_credentials(f)
    f.write_bytes(DPAPI_MAGIC + b" scope=machine\n!!!not base64!!!\n")
    with pytest.raises(CredentialError, match="damaged"):
        load_credentials(f)


@win
def test_set_camera_credentials_cli_prompts_and_never_prints_secret(tmp_path: Path,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    from sentinel.winsvc.__main__ import cmd_set_credentials

    f = tmp_path / "camera.dpapi"
    args = argparse.Namespace(file=str(f), config=None, scope="machine", no_acl=True)
    answers = iter(["S3cretPW!", "S3cretPW!"])
    rc = cmd_set_credentials(args, prompt=lambda _: "viewer", secret_prompt=lambda _: next(answers))
    out = capsys.readouterr().out
    assert rc == 0 and "S3cretPW!" not in out and "viewer" not in out
    assert load_credentials(f).password == "S3cretPW!"
    mismatch = iter(["a1", "a2"])
    assert cmd_set_credentials(args, prompt=lambda _: "viewer", secret_prompt=lambda _: next(mismatch)) == 1
    assert load_credentials(f).password == "S3cretPW!"          # unchanged
