"""Read-only ONVIF probe: what the camera says about itself (``Sentinel.exe onvif-probe``, 0.3.22).

ONVIF is the protocol most IP cameras speak beside their own web page. This module **asks** the camera a
handful of standard questions and writes the answers down. It changes nothing on the camera: every request is
a ``Get...`` (the names are in :data:`QUESTIONS`; a test checks that nothing else can be sent).

Why (owner request 2026-11-18): to find out, on the site's own camera, whether Sentinel could later record
the camera's settings in the recording log and listen for the camera's own motion and tamper events, without
learning each maker's software. Cameras differ in the details, so the report ends with the camera's raw
answers for whoever adapts Sentinel to it.

Signing in. Two standard ways, both used at once because cameras differ in which they want:

* WS-Security UsernameToken with a password digest (SHA-1 over nonce, time and password; ONVIF's original);
* HTTP digest authentication (RFC 7616) with MD5 or SHA-256, which is what a camera's "ONVIF: MD5 / SHA-256"
  setting chooses. The stronger one the camera offers is used.

The password is never sent as it is, never written to the report and never logged.

Only the Python standard library is used. Answers are capped at :data:`MAX_ANSWER_BYTES`, and an answer that
carries a document type declaration is refused (no entity tricks from a device on the network).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

MAX_ANSWER_BYTES = 2_000_000
TIMEOUT_S = 6.0
DEFAULT_PORTS = (80, 8080, 8000, 8899, 2020)        # where cameras usually listen for ONVIF
DEVICE_PATH = "/onvif/device_service"

NS = {
    "device": "http://www.onvif.org/ver10/device/wsdl",
    "media": "http://www.onvif.org/ver10/media/wsdl",
    "media2": "http://www.onvif.org/ver20/media/wsdl",
    "events": "http://www.onvif.org/ver10/events/wsdl",
    "imaging": "http://www.onvif.org/ver20/imaging/wsdl",
}
# Every request this module can send: (service, name). All of them only read.
QUESTIONS = (
    ("device", "GetSystemDateAndTime"), ("device", "GetDeviceInformation"), ("device", "GetScopes"),
    ("device", "GetServices"), ("device", "GetCapabilities"),
    ("media", "GetProfiles"), ("media", "GetStreamUri"), ("media", "GetVideoSources"),
    ("events", "GetEventProperties"), ("imaging", "GetImagingSettings"),
)
_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_PW_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
_B64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
_SOAP = "http://www.w3.org/2003/05/soap-envelope"


class OnvifError(Exception):
    """The camera did not give a usable answer. The message is for the operator."""


# ---------------------------------------------------------------------- signing in (pure)
def password_digest(nonce: bytes, created: str, password: str) -> str:
    """WS-Security UsernameToken: Base64(SHA-1(nonce + created + password))."""
    return base64.b64encode(hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()).decode()


def security_header(username: str, password: str, now: float, nonce: bytes | None = None) -> str:
    nonce = os.urandom(16) if nonce is None else nonce
    created = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (f'<wsse:Security xmlns:wsse="{_WSSE}" xmlns:wsu="{_WSU}"><wsse:UsernameToken>'
            f"<wsse:Username>{_xml(username)}</wsse:Username>"
            f'<wsse:Password Type="{_PW_DIGEST}">{password_digest(nonce, created, password)}</wsse:Password>'
            f'<wsse:Nonce EncodingType="{_B64}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
            f"<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>")


_CHALLENGE = re.compile(r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')
_HASHES: dict[str, Callable[[bytes], Any]] = {"MD5": hashlib.md5, "SHA-256": hashlib.sha256}


def parse_challenges(headers: list[str]) -> list[dict[str, str]]:
    """``WWW-Authenticate`` header values -> the Digest challenges in them (one per header or several in one)."""
    out = []
    for h in headers:
        for part in re.split(r"(?i)(?:^|,\s*)(?=digest\s)", h):
            part = part.strip()
            if not part.lower().startswith("digest"):
                continue
            fields = {m[1].lower(): (m[2] if m[2] is not None else m[3]) for m in _CHALLENGE.finditer(part[6:])}
            if fields.get("nonce"):
                out.append(fields)
    return out


def pick_challenge(challenges: list[dict[str, str]]) -> dict[str, str] | None:
    """The strongest challenge this module can answer: SHA-256 before MD5 (no algorithm named = MD5)."""
    def algo(c: dict[str, str]) -> str:
        return (c.get("algorithm") or "MD5").upper()

    usable = [c for c in challenges if algo(c) in _HASHES and "auth" in (c.get("qop") or "auth").lower()]
    usable.sort(key=lambda c: algo(c) != "SHA-256")
    return usable[0] if usable else None


def digest_authorization(challenge: dict[str, str], username: str, password: str, method: str, uri: str,
                         cnonce: str | None = None, nc: int = 1) -> str:
    """The ``Authorization`` header value answering an HTTP digest challenge (RFC 7616, qop=auth or none)."""
    algorithm = (challenge.get("algorithm") or "MD5").upper()
    h = _HASHES[algorithm]

    def hexd(text: str) -> str:
        return h(text.encode("utf-8")).hexdigest()

    realm, nonce = challenge.get("realm", ""), challenge["nonce"]
    ha1, ha2 = hexd(f"{username}:{realm}:{password}"), hexd(f"{method}:{uri}")
    fields = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"',
              f"algorithm={algorithm}"]
    if challenge.get("qop"):
        cnonce = cnonce or os.urandom(8).hex()
        count = f"{nc:08x}"
        fields += [f'response="{hexd(f"{ha1}:{nonce}:{count}:{cnonce}:auth:{ha2}")}"', "qop=auth", f"nc={count}",
                   f'cnonce="{cnonce}"']
    else:
        fields.append(f'response="{hexd(f"{ha1}:{nonce}:{ha2}")}"')
    if challenge.get("opaque"):
        fields.append(f'opaque="{challenge["opaque"]}"')
    return "Digest " + ", ".join(fields)


# ---------------------------------------------------------------------- asking
def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def envelope(service: str, name: str, inner: str = "", security: str = "") -> bytes:
    if (service, name) not in QUESTIONS:
        raise ValueError(f"{name} is not one of the questions this probe may ask")
    head = f"<s:Header>{security}</s:Header>" if security else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{_SOAP}">{head}<s:Body>'
            f'<q:{name} xmlns:q="{NS[service]}">{inner}</q:{name}></s:Body></s:Envelope>').encode("utf-8")


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find(node: ET.Element | None, *names: str) -> ET.Element | None:
    """The first descendant reached by following these local names (prefixes and namespaces differ by maker)."""
    for name in names:
        if node is None:
            return None
        node = next((e for e in node.iter() if local(e.tag) == name and e is not node), None)
    return node


def findall(node: ET.Element | None, name: str) -> list[ET.Element]:
    return [] if node is None else [e for e in node.iter() if local(e.tag) == name and e is not node]


def text(node: ET.Element | None, *names: str) -> str:
    e = find(node, *names) if names else node
    return (e.text or "").strip() if e is not None else ""


def parse_answer(data: bytes) -> ET.Element:
    if len(data) > MAX_ANSWER_BYTES:
        raise OnvifError("the camera's answer is too large to be an ONVIF answer")
    if b"<!DOCTYPE" in data[:4096].upper() or b"<!ENTITY" in data.upper():
        raise OnvifError("the camera's answer is not plain ONVIF (it declares a document type)")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise OnvifError(f"the camera's answer is not XML ({exc})") from None
    fault = find(root, "Fault")
    if fault is not None:
        reason = text(fault, "Reason", "Text") or text(fault, "faultstring") or "no reason given"
        code = text(fault, "Subcode", "Value") or text(fault, "Code", "Value")
        raise OnvifError(f"the camera refused: {reason}" + (f" ({code})" if code else ""))
    return root


@dataclass
class Answer:
    service: str
    name: str
    url: str
    root: ET.Element | None = None
    raw: bytes = b""
    error: str = ""
    signed_in_with: str = ""


@dataclass
class Camera:
    """One camera's ONVIF services. ``opener``: (url, body, headers, timeout) -> (status, headers, body);
    replaced in tests."""
    host: str
    port: int
    username: str = ""
    password: str = ""
    clock_offset_s: float = 0.0                      # camera clock minus this computer's
    services: dict[str, str] = field(default_factory=dict)
    answers: list[Answer] = field(default_factory=list)
    opener: Callable[..., tuple[int, Any, bytes]] | None = None
    now: Callable[[], float] = time.time

    def url(self, service: str) -> str:
        return self.services.get(service) or f"http://{self.host}:{self.port}{DEVICE_PATH}"

    def ask(self, service: str, name: str, inner: str = "", sign: bool = True) -> Answer:
        url = self.url(service)
        ans = Answer(service, name, url)
        self.answers.append(ans)
        security = ""
        if sign and self.username:
            security = security_header(self.username, self.password, self.now() + self.clock_offset_s)
            ans.signed_in_with = "WS-UsernameToken"
        body = envelope(service, name, inner, security)
        headers = {"Content-Type": f'application/soap+xml; charset=utf-8; action="{NS[service]}/{name}"'}
        try:
            status, got, data = self._post(url, body, headers)
            if status == 401 and self.username:
                challenge = pick_challenge(parse_challenges(_all(got, "WWW-Authenticate")))
                if challenge is not None:
                    path = urlsplit(url).path or "/"
                    headers["Authorization"] = digest_authorization(challenge, self.username, self.password, "POST", path)
                    ans.signed_in_with = f"HTTP digest {(challenge.get('algorithm') or 'MD5').upper()}"
                    status, got, data = self._post(url, body, headers)
            ans.raw = data
            if status == 401:
                raise OnvifError("the camera did not accept the account (HTTP 401)")
            if status >= 400 and b"Envelope" not in data:
                raise OnvifError(f"the camera answered HTTP {status}")
            ans.root = parse_answer(data)
        except OnvifError as exc:
            ans.error = str(exc)
        except (OSError, ValueError) as exc:
            ans.error = f"no answer ({type(exc).__name__}: {exc})"
        return ans

    def _post(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[int, Any, bytes]:
        if self.opener is not None:
            return self.opener(url, body, headers, TIMEOUT_S)
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            # No proxy, no redirects to other hosts: the camera is asked directly.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=TIMEOUT_S) as resp:
                return resp.status, resp.headers, resp.read(MAX_ANSWER_BYTES + 1)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read(MAX_ANSWER_BYTES + 1)


def _all(headers: Any, name: str) -> list[str]:
    if headers is None:
        return []
    if hasattr(headers, "get_all"):
        return list(headers.get_all(name) or [])
    return [v for k, v in dict(headers).items() if k.lower() == name.lower()]


# ---------------------------------------------------------------------- the probe
def _camera_time(root: ET.Element | None) -> float | None:
    utc = find(root, "UTCDateTime")
    try:
        d, t = find(utc, "Date"), find(utc, "Time")
        return datetime(int(text(d, "Year")), int(text(d, "Month")), int(text(d, "Day")), int(text(t, "Hour")),
                        int(text(t, "Minute")), int(text(t, "Second")), tzinfo=UTC).timestamp()
    except (TypeError, ValueError):
        return None


def _scopes(root: ET.Element | None) -> list[str]:
    return [text(e) for e in findall(root, "ScopeItem")]


def _profile_names(scopes: list[str]) -> list[str]:
    names = {"streaming": "S", "t": "T", "g": "G", "q": "Q", "c": "C", "a": "A", "m": "M", "d": "D"}
    out = []
    for s in scopes:
        m = re.search(r"onvif://www\.onvif\.org/Profile/([^/\s]+)", s, re.I)
        if m:
            out.append("Profile " + names.get(m[1].lower(), m[1]))
    return sorted(set(out))


def _service_addresses(cam: Camera, services: ET.Element | None, caps: ET.Element | None) -> None:
    wanted = {NS["media"]: "media", NS["media2"]: "media2", NS["events"]: "events", NS["imaging"]: "imaging",
              "http://www.onvif.org/ver20/analytics/wsdl": "analytics", "http://www.onvif.org/ver20/ptz/wsdl": "ptz"}
    for svc in findall(services, "Service"):
        key = wanted.get(text(svc, "Namespace"))
        if key and text(svc, "XAddr"):
            cam.services.setdefault(key, text(svc, "XAddr"))
    for tag, key in (("Media", "media"), ("Events", "events"), ("Imaging", "imaging"), ("Analytics", "analytics"),
                     ("PTZ", "ptz")):
        addr = text(find(caps, tag), "XAddr")
        if addr:
            cam.services.setdefault(key, addr)


def _stream_line(p: ET.Element) -> str:
    v = find(p, "VideoEncoderConfiguration")
    a = find(p, "AudioEncoderConfiguration")
    parts = []
    if v is not None:
        res = find(v, "Resolution")
        rate = find(v, "RateControl")
        gov = text(v, "GovLength")
        parts.append(f"{text(v, 'Encoding') or '?'} {text(res, 'Width')}x{text(res, 'Height')}, "
                     f"up to {text(rate, 'FrameRateLimit') or '?'} pictures/s, "
                     f"up to {text(rate, 'BitrateLimit') or '?'} kbit/s"
                     + (f", key frame every {gov} pictures" if gov else ""))
    else:
        parts.append("no video settings given")
    if a is not None:
        parts.append(f"sound: {text(a, 'Encoding') or '?'} {text(a, 'SampleRate') or '?'} kHz, "
                     f"{text(a, 'Bitrate') or '?'} kbit/s")
    else:
        parts.append("no sound")
    return "; ".join(parts)


MOTION_WORDS = ("motion", "celldetector", "fielddetector")
TAMPER_WORDS = ("tamper", "globalscenechange", "imagetoodark", "imagetoobright", "imagetooblurry", "signalloss")


def _topics(root: ET.Element | None) -> list[str]:
    """Topic paths under TopicSet whose node is marked as a topic (``wstop:topic="true"``)."""
    out: list[str] = []

    def walk(node: ET.Element, path: str) -> None:
        for child in node:
            name = local(child.tag)
            if name in ("MessageDescription",):
                continue
            here = f"{path}/{name}" if path else name
            if any(local(k) == "topic" and v == "true" for k, v in child.attrib.items()):
                out.append(here)
            walk(child, here)

    ts = find(root, "TopicSet")
    if ts is not None:
        walk(ts, "")
    return out


def probe(host: str, username: str = "", password: str = "", ports: tuple[int, ...] = DEFAULT_PORTS,
          opener: Callable[..., tuple[int, Any, bytes]] | None = None, version: str = "",
          now: Callable[[], float] = time.time) -> tuple[int, str]:
    """Ask the camera at ``host`` and return (exit code, report text). 0 = it answered as an ONVIF camera,
    4 = no ONVIF answer on any port tried, 5 = it answered but did not accept the account."""
    lines: list[str] = []
    out = lines.append
    stamp = datetime.fromtimestamp(now(), UTC).strftime("%Y-%m-%d %H:%M UTC")
    out(f"ONVIF PROBE of {host}" + (f" (Sentinel {version})" if version else "") + f", {stamp}")
    out("Read-only: the camera was only asked questions. Nothing on it was changed.")
    out("")
    cam = None
    tried = []
    for port in ports:
        c = Camera(host, port, username, password, opener=opener, now=now)
        first = c.ask("device", "GetSystemDateAndTime", sign=False)
        tried.append(f"port {port}: " + ("answered" if first.root is not None else first.error))
        if first.root is not None:
            cam = c
            break
    out("Where ONVIF was looked for: " + "; ".join(tried))
    if cam is None:
        out("")
        out("RESULT: no ONVIF answer. Check that ONVIF is switched on in the camera's own settings page, and give")
        out("the port shown there with --port if it is not one of those tried.")
        return 4, "\n".join(lines) + "\n"
    t = _camera_time(cam.answers[0].root)
    if t is not None:
        cam.clock_offset_s = t - now()
        ahead = "ahead of" if cam.clock_offset_s >= 0 else "behind"
        out(f"Camera clock: {datetime.fromtimestamp(t, UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC "
            f"({abs(cam.clock_offset_s):.0f} s {ahead} this computer)")
    else:
        out("Camera clock: not given in a form this probe reads")
    info = cam.ask("device", "GetDeviceInformation")
    if info.root is None:
        out("")
        out(f"RESULT: the camera speaks ONVIF on port {cam.port}, but would not say more: {info.error}.")
        out("Account used: " + (f"'{username}'" if username else "none") + ". Many cameras keep ONVIF accounts apart "
            "from the web page's accounts: look for an ONVIF user in the camera's settings, and give it with --user.")
        return 5, "\n".join(lines) + "\n" + _raw(cam)
    d = find(info.root, "GetDeviceInformationResponse")
    out(f"Signed in as: {username or '(no account needed)'}" + (f", by {info.signed_in_with}" if username else ""))
    out("Device: " + ", ".join(f"{k} {text(d, k) or '?'}" for k in ("Manufacturer", "Model", "FirmwareVersion",
                                                                    "SerialNumber", "HardwareId")))
    scopes = _scopes(cam.ask("device", "GetScopes").root)
    out("Says it conforms to: " + (", ".join(_profile_names(scopes)) or "no profile named"))
    services = cam.ask("device", "GetServices", "<q:IncludeCapability>false</q:IncludeCapability>")
    caps = cam.ask("device", "GetCapabilities", "<q:Category>All</q:Category>")
    _service_addresses(cam, services.root, caps.root)
    out("Services: " + (", ".join(f"{k} ({v})" for k, v in sorted(cam.services.items())) or "none listed"))
    out("")
    out("STREAMS (ONVIF calls each a profile)")
    profiles = cam.ask("media", "GetProfiles") if "media" in cam.services else None
    plist = findall(profiles.root, "Profiles") if profiles is not None else []
    if not plist:
        out("  none listed" + (f": {profiles.error}" if profiles is not None and profiles.error else ""))
    for i, p in enumerate(plist, 1):
        token = p.attrib.get("token", "")
        out(f"  {i}. {text(p, 'Name') or '(no name)'} [{token}]: {_stream_line(p)}")
        uri = cam.ask("media", "GetStreamUri",
                      '<q:StreamSetup><t:Stream xmlns:t="http://www.onvif.org/ver10/schema">RTP-Unicast</t:Stream>'
                      '<t:Transport xmlns:t="http://www.onvif.org/ver10/schema"><t:Protocol>RTSP</t:Protocol>'
                      f"</t:Transport></q:StreamSetup><q:ProfileToken>{_xml(token)}</q:ProfileToken>")
        out("     address: " + (_no_account(text(uri.root, "Uri")) or f"not given ({uri.error or 'empty'})"))
    out("")
    out("EVENTS THE CAMERA CAN SEND")
    ev = cam.ask("events", "GetEventProperties") if "events" in cam.services else None
    topics = _topics(ev.root) if ev is not None else []
    if not topics:
        out("  none listed" + (f": {ev.error}" if ev is not None and ev.error else
                               "" if ev is not None else ": the camera lists no events service"))
    else:
        motion = [x for x in topics if any(w in x.lower().replace("/", "") for w in MOTION_WORDS)]
        tamper = [x for x in topics if any(w in x.lower().replace("/", "") for w in TAMPER_WORDS)]
        out("  motion: " + (", ".join(motion) or "none"))
        out("  tamper or picture trouble: " + (", ".join(tamper) or "none"))
        out(f"  all {len(topics)}: " + ", ".join(topics))
    out("")
    out("PICTURE SETTINGS")
    src = cam.ask("media", "GetVideoSources") if "media" in cam.services else None
    token = next((e.attrib.get("token", "") for e in findall(src.root if src else None, "VideoSources")), "")
    img = (cam.ask("imaging", "GetImagingSettings", f"<q:VideoSourceToken>{_xml(token)}</q:VideoSourceToken>")
           if token and "imaging" in cam.services else None)
    settings = find(img.root, "ImagingSettings") if img is not None else None
    if settings is None:
        out("  not given" + (f": {img.error}" if img is not None and img.error else ""))
    else:
        shown = []
        for child in settings:
            value = (child.text or "").strip() or ", ".join(f"{local(g.tag)} {(g.text or '').strip()}" for g in child
                                                           if (g.text or "").strip())
            shown.append(f"{local(child.tag)}: {value or '(set)'}")
        out("  " + "; ".join(shown))
    out("")
    failed = [f"{a.name}: {a.error}" for a in cam.answers if a.error]
    out("WHAT DID NOT WORK: " + ("; ".join(failed) if failed else "nothing; every question was answered"))
    out("")
    out(f"RESULT: the camera answers as an ONVIF device on port {cam.port}.")
    return 0, "\n".join(lines) + "\n" + _raw(cam)


def _no_account(uri: str) -> str:
    """A stream address without any account a camera may have put into it."""
    parts = urlsplit(uri)
    if "@" not in parts.netloc:
        return uri
    return uri.replace(parts.netloc, parts.netloc.rsplit("@", 1)[1], 1)


def _raw(cam: Camera) -> str:
    """The camera's answers as it gave them, for whoever adapts Sentinel to this camera. Requests are not
    included (they hold the sign-in digest), and neither is the password."""
    out = ["", "-" * 78, "THE CAMERA'S ANSWERS, AS RECEIVED (for technical support)", "-" * 78]
    for a in cam.answers:
        out.append(f"### {a.service} {a.name}  {a.url}" + (f"  [{a.error}]" if a.error else ""))
        # Accounts inside stream addresses go first (user and password together); then the password wherever
        # else a camera may repeat it, as it is and as XML writes it.
        body = _no_accounts_in(a.raw[:60_000].decode("utf-8", "replace"))
        for secret in {cam.password, _xml(cam.password)} - {""}:
            body = body.replace(secret, "<password removed>")
        out.append(body)
        out.append("")
    return "\n".join(out) + "\n"


def _no_accounts_in(body: str) -> str:
    return re.sub(r"((?:rtsps?|https?)://)[^/@\s<>\"']+@", r"\1", body)


RAW_STARTS = "\n" + "-" * 78


# ---------------------------------------------------------------------- the command
def run_probe(cfg: Any, host: str | None = None, port: int | None = None, user: str | None = None,
              out_file: str | None = None, out: Callable[[str], None] = print,
              ask_password: Callable[[str], str] | None = None,
              opener: Callable[..., tuple[int, Any, bytes]] | None = None) -> int:
    """``Sentinel.exe onvif-probe``: ask the configured camera (or ``--host``) and show what it says. The
    account is the one Sentinel already uses for the camera, or ``--user`` (its password is asked for, not
    shown and not kept). The report is also saved to a file to send to technical support."""
    from pathlib import Path

    from . import __version__
    from .credentials import CredentialError, load_credentials, register_secret

    if not host:
        if cfg.camera.pending or not cfg.camera.url:
            out("No camera has been added yet (add it on the Camera page first, or give --host).")
            return 2
        host = urlsplit(cfg.camera.url).hostname or ""
    if not host:
        out("The camera address has no host name; give it with --host.")
        return 2
    username = password = ""
    if user:
        if ask_password is None:
            import getpass

            ask_password = getpass.getpass
        username, password = user, ask_password(f"Password of the camera account '{user}' (not shown): ")
        register_secret(password)
    elif cfg.camera.credentials_file:
        try:
            creds = load_credentials(cfg.camera.credentials_file)
            username, password = creds.username, creds.password
        except CredentialError as exc:
            out(f"The camera credentials cannot be read: {exc}")
            return 2
    out(f"Asking the camera at {host} (a few seconds; nothing on it is changed)...")
    code, report = probe(host, username, password, (port,) if port else DEFAULT_PORTS, opener=opener,
                         version=__version__)
    out(report.split(RAW_STARTS, 1)[0].rstrip())
    name = f"onvif-probe-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    places = ([Path(out_file)] if out_file else []) + [cfg.log_path / name, Path.home() / "Documents" / name,
                                                       Path.cwd() / name]
    for p in places:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(report.replace("\n", "\r\n"), encoding="utf-8", newline="")
        except OSError:
            continue
        out("")
        out(f"The full report, with the camera's answers as it gave them, is saved as:\n  {p}")
        out("Send that file to technical support. It holds no password.")
        break
    else:
        out("")
        out("The report could not be saved to a file; copy the lines above instead.")
    return code
