"""Camera credential handling and redaction.

Credentials live in a separate file readable only by the service account. Two formats:

* plain TOML (Linux, and fallback on Windows)::

      username = "viewer"
      password = "..."

* on Windows, the same TOML protected with DPAPI (written by
  ``python -m sentinel.winsvc set-camera-credentials``): a first line starting with
  :data:`DPAPI_MAGIC`, then the base64 DPAPI blob. Detected by content, so the configuration
  only names the file (``credentials_file``) either way.

They are injected into the RTSP URL only when building the FFmpeg argv, and every log
record passes through :func:`redact`. See docs/02 risk B4 for the residual argv exposure.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

DPAPI_MAGIC = b"SENTINEL-DPAPI-CREDENTIALS v1"


class CredentialError(ValueError):
    pass


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str
    protection: str = "plaintext"          # "plaintext" | "dpapi"

    def __repr__(self) -> str:  # never print secrets, even in tracebacks
        return f"Credentials(username='***', password='***', protection={self.protection!r})"


def credentials_toml(username: str, password: str) -> str:
    """Serialise credentials as the TOML credentials format (JSON string escapes are valid TOML)."""
    return f"username = {json.dumps(username, ensure_ascii=False)}\npassword = {json.dumps(password, ensure_ascii=False)}\n"


def _decrypt_dpapi_file(data: bytes, p: Path) -> str:
    lines = data.splitlines()
    if len(lines) < 2:
        raise CredentialError(f"protected credentials file is incomplete: {p}")
    try:
        blob = base64.b64decode(b"".join(lines[1:]), validate=True)
    except (binascii.Error, ValueError):
        raise CredentialError(f"protected credentials file is damaged: {p}") from None
    from .winsvc.dpapi import DpapiError, unprotect

    try:
        plain = unprotect(blob)
    except DpapiError as exc:
        raise CredentialError(f"protected credentials file cannot be decrypted on this computer: {p} "
                              f"({exc.strerror or exc})") from None
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        raise CredentialError(f"protected credentials file is damaged: {p}") from None


def load_credentials(path: str | Path) -> Credentials:
    p = Path(path)
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        raise CredentialError(f"credentials file not found: {p}") from None
    except OSError as exc:
        raise CredentialError(f"credentials file cannot be read: {p} ({exc.strerror})") from None
    protection = "plaintext"
    if data.startswith(DPAPI_MAGIC):
        text = _decrypt_dpapi_file(data, p)
        protection = "dpapi"
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise CredentialError(f"credentials file is not valid TOML: {p}") from None
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise CredentialError(f"credentials file is not valid TOML: {p}") from None
    user, pw = raw.get("username"), raw.get("password")
    if not isinstance(user, str) or not isinstance(pw, str) or not user:
        raise CredentialError("credentials file must define text 'username' and 'password'")
    extra = set(raw) - {"username", "password"}
    if extra:
        raise CredentialError("credentials file has unexpected keys")
    return Credentials(user, pw, protection)


def url_with_credentials(url: str, creds: Credentials | None) -> str:
    if creds is None:
        return url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = f"{quote(creds.username, safe='')}:{quote(creds.password, safe='')}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# scheme://user:pass@host  ->  scheme://***:***@host. Greedy up to the last '@' before the
# path, so a raw '@' inside a password cannot leak its tail.
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^/\s]*)@")
# OEM-style credentials in path/query (rejected by config, scrubbed here as defence in depth).
_KV_SECRET = re.compile(r"(?i)\b(user|username|usr|password|passwd|pass|pwd|token|auth)=([^&;\s/]*)")
_registered_secrets: set[str] = set()


def register_secret(value: str) -> None:
    """Register a literal secret so it is scrubbed even outside URL syntax."""
    if value and len(value) >= 3:
        _registered_secrets.add(value)
        _registered_secrets.add(quote(value, safe=""))


def redact(text: str) -> str:
    out = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}***:***@", text)
    out = _KV_SECRET.sub(lambda m: f"{m.group(1)}=***", out)
    for secret in sorted(_registered_secrets, key=len, reverse=True):
        out = out.replace(secret, "***")
    return out
