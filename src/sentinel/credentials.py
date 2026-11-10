"""Camera credential handling and redaction.

Credentials live in a separate TOML file readable only by the service account::

    username = "viewer"
    password = "..."

They are injected into the RTSP URL only when building the FFmpeg argv, and every log
record passes through :func:`redact`. See docs/02 risk B4 for the residual argv exposure.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


class CredentialError(ValueError):
    pass


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str

    def __repr__(self) -> str:  # never print secrets, even in tracebacks
        return "Credentials(username='***', password='***')"


def load_credentials(path: str | Path) -> Credentials:
    p = Path(path)
    try:
        with p.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise CredentialError(f"credentials file not found: {p}") from None
    except tomllib.TOMLDecodeError:
        raise CredentialError(f"credentials file is not valid TOML: {p}") from None
    user, pw = raw.get("username"), raw.get("password")
    if not isinstance(user, str) or not isinstance(pw, str) or not user:
        raise CredentialError("credentials file must define text 'username' and 'password'")
    extra = set(raw) - {"username", "password"}
    if extra:
        raise CredentialError("credentials file has unexpected keys")
    return Credentials(user, pw)


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
