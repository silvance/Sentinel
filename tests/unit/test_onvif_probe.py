"""Read-only ONVIF probe (owner request 2026-11-18, 0.3.22): ask the camera what it says about itself and
write it down, to find out on the site's own camera what Sentinel could read from it. Tried here against a
pretend camera; never against the real one."""

from __future__ import annotations

import dataclasses
import re
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from conftest import make_config
from sentinel import onvif
from sentinel.launcher.cli import build_parser
from sentinel.onvif import (QUESTIONS, OnvifError, digest_authorization, envelope, parse_answer, parse_challenges,
                            password_digest, pick_challenge, probe, run_probe)

NOW = datetime(2026, 11, 19, 4, 40, 0, tzinfo=UTC).timestamp()
SOAP = 'xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"'
TT = 'xmlns:tt="http://www.onvif.org/ver10/schema"'


def wrap(inner: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope {SOAP} {TT} '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
            'xmlns:tev="http://www.onvif.org/ver10/events/wsdl" xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl">'
            f"<SOAP-ENV:Body>{inner}</SOAP-ENV:Body></SOAP-ENV:Envelope>").encode()


def fault(reason: str) -> bytes:
    return wrap("<SOAP-ENV:Fault><SOAP-ENV:Code><SOAP-ENV:Value>SOAP-ENV:Receiver</SOAP-ENV:Value><SOAP-ENV:Subcode>"
                "<SOAP-ENV:Value>ter:ActionNotSupported</SOAP-ENV:Value></SOAP-ENV:Subcode></SOAP-ENV:Code>"
                f'<SOAP-ENV:Reason><SOAP-ENV:Text xml:lang="en">{reason}</SOAP-ENV:Text></SOAP-ENV:Reason></SOAP-ENV:Fault>')


def answers(base: str, hour: int = 4) -> dict[str, bytes]:
    return {
        "GetSystemDateAndTime": wrap(
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:DateTimeType>NTP</tt:DateTimeType>"
            "<tt:LocalDateTime><tt:Time><tt:Hour>23</tt:Hour><tt:Minute>40</tt:Minute><tt:Second>2</tt:Second></tt:Time>"
            "<tt:Date><tt:Year>2026</tt:Year><tt:Month>11</tt:Month><tt:Day>18</tt:Day></tt:Date></tt:LocalDateTime>"
            f"<tt:UTCDateTime><tt:Time><tt:Hour>{hour}</tt:Hour><tt:Minute>40</tt:Minute><tt:Second>2</tt:Second></tt:Time>"
            "<tt:Date><tt:Year>2026</tt:Year><tt:Month>11</tt:Month><tt:Day>19</tt:Day></tt:Date></tt:UTCDateTime>"
            "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"),
        "GetDeviceInformation": wrap(
            "<tds:GetDeviceInformationResponse><tds:Manufacturer>KT&amp;C</tds:Manufacturer><tds:Model>KNC-HMSS347"
            "</tds:Model><tds:FirmwareVersion>1.2.3</tds:FirmwareVersion><tds:SerialNumber>SN123</tds:SerialNumber>"
            "<tds:HardwareId>HW1</tds:HardwareId></tds:GetDeviceInformationResponse>"),
        "GetScopes": wrap(
            "<tds:GetScopesResponse>" + "".join(
                f"<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>{s}</tt:ScopeItem></tds:Scopes>"
                for s in ("onvif://www.onvif.org/Profile/Streaming", "onvif://www.onvif.org/Profile/T",
                          "onvif://www.onvif.org/name/KNC")) + "</tds:GetScopesResponse>"),
        "GetServices": wrap(
            "<tds:GetServicesResponse>" + "".join(
                f"<tds:Service><tds:Namespace>http://www.onvif.org/{ns}</tds:Namespace><tds:XAddr>{base}/onvif/{path}"
                "</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>60</tt:Minor></tds:Version></tds:Service>"
                for ns, path in (("ver10/device/wsdl", "device_service"), ("ver10/media/wsdl", "media_service"),
                                 ("ver10/events/wsdl", "event_service"), ("ver20/imaging/wsdl", "imaging_service")))
            + "</tds:GetServicesResponse>"),
        "GetCapabilities": fault("Optional Action Not Implemented"),
        "GetProfiles": wrap(
            "<trt:GetProfilesResponse>" + "".join(
                f'<trt:Profiles token="prof{i}" fixed="true"><tt:Name>Stream{i + 1}</tt:Name>'
                f'<tt:VideoEncoderConfiguration token="ve{i}"><tt:Name>ve{i}</tt:Name><tt:Encoding>H264</tt:Encoding>'
                f"<tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution><tt:Quality>5"
                "</tt:Quality><tt:RateControl><tt:FrameRateLimit>30</tt:FrameRateLimit><tt:EncodingInterval>1"
                f"</tt:EncodingInterval><tt:BitrateLimit>{kbit}</tt:BitrateLimit></tt:RateControl><tt:H264>"
                "<tt:GovLength>30</tt:GovLength><tt:H264Profile>High</tt:H264Profile></tt:H264>"
                "</tt:VideoEncoderConfiguration>" + sound + "</trt:Profiles>"
                for i, (w, h, kbit, sound) in enumerate((
                    (1920, 1080, 4000, '<tt:AudioEncoderConfiguration token="ae0"><tt:Name>ae</tt:Name><tt:Encoding>G711'
                                       "</tt:Encoding><tt:Bitrate>64</tt:Bitrate><tt:SampleRate>8</tt:SampleRate>"
                                       "</tt:AudioEncoderConfiguration>"),
                    (1280, 720, 1000, "")))) + "</trt:GetProfilesResponse>"),
        "GetVideoSources": wrap('<trt:GetVideoSourcesResponse><trt:VideoSources token="vs0"><tt:Framerate>30</tt:Framerate>'
                                "</trt:VideoSources></trt:GetVideoSourcesResponse>"),
        "GetEventProperties": wrap(
            "<tev:GetEventPropertiesResponse><tev:TopicNamespaceLocation>http://www.onvif.org/onvif/ver10/topics/"
            'topicns.xml</tev:TopicNamespaceLocation><wstop:TopicSet xmlns:wstop="http://docs.oasis-open.org/wsn/t-1" '
            'xmlns:tns1="http://www.onvif.org/ver10/topics"><tns1:RuleEngine><CellMotionDetector><Motion wstop:topic="true">'
            '<tt:MessageDescription IsProperty="true"><tt:Source><tt:SimpleItemDescription Name="Rule" Type="xs:string"/>'
            "</tt:Source></tt:MessageDescription></Motion></CellMotionDetector><TamperDetector>"
            '<Tamper wstop:topic="true"/></TamperDetector></tns1:RuleEngine><tns1:VideoSource><MotionAlarm '
            'wstop:topic="true"/><GlobalSceneChange><ImagingService wstop:topic="true"/></GlobalSceneChange>'
            '</tns1:VideoSource><tns1:Device><Trigger><DigitalInput wstop:topic="true"/></Trigger></tns1:Device>'
            "</wstop:TopicSet></tev:GetEventPropertiesResponse>"),
        "GetImagingSettings": wrap(
            "<timg:GetImagingSettingsResponse><timg:ImagingSettings><tt:Brightness>50</tt:Brightness><tt:IrCutFilter>AUTO"
            "</tt:IrCutFilter><tt:Exposure><tt:Mode>AUTO</tt:Mode><tt:Gain>10</tt:Gain></tt:Exposure>"
            "</timg:ImagingSettings></timg:GetImagingSettingsResponse>"),
    }


class Headers:
    def __init__(self, items: list[tuple[str, str]]) -> None:
        self.items = items

    def get_all(self, name: str) -> list[str]:
        return [v for k, v in self.items if k.lower() == name.lower()]


class PretendCamera:
    """Answers like an ONVIF camera. ``auth``: "none", "ws" (UsernameToken only), "digest" (HTTP digest; the
    algorithms it offers are in ``offers``). Remembers what it was asked."""

    def __init__(self, auth: str = "digest", offers: tuple[str, ...] = ("MD5", "SHA-256"), user: str = "viewer",
                 password: str = "s3cret-pw", base: str = "http://192.168.0.10", hour: int = 4) -> None:
        self.auth, self.offers, self.user, self.password = auth, offers, user, password
        self.answers = answers(base, hour)
        self.camera_time = f"2026-11-19T{hour:02d}:40:02Z"
        self.asked: list[tuple[str, str]] = []
        self.signed_in: list[str] = []
        self.bodies: list[bytes] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str], _timeout: float) -> tuple[int, Any, bytes]:
        name = re.search(rb"<q:(\w+)[ >]", body)[1].decode()
        self.asked.append((url, name))
        self.bodies.append(body)
        assert name.startswith("Get"), name                      # the probe only reads
        if name != "GetSystemDateAndTime" and self.auth != "none":
            if not self._allowed(url, body, headers):
                www = [("WWW-Authenticate", f'Digest realm="onvif", qop="auth", nonce="n0nce{a}", opaque="0paque", '
                                            f"algorithm={a}") for a in self.offers] if self.auth == "digest" else []
                return 401, Headers(www), b"<html>401 Unauthorized</html>"
        if name == "GetStreamUri":
            token = re.search(rb"ProfileToken>([^<]+)<", body)[1].decode()
            n = token[-1]
            return 200, Headers([]), wrap(
                f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://{self.user}:{self.password}@192.168.0.10:554/"
                f"stream{n}</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect></trt:MediaUri>"
                "</trt:GetStreamUriResponse>")
        data = self.answers[name]
        return (500 if b"Fault>" in data else 200), Headers([]), data

    def _allowed(self, url: str, body: bytes, headers: dict[str, str]) -> bool:
        if self.auth == "ws":
            m = re.search(rb"<wsse:Username>([^<]*)</wsse:Username><wsse:Password[^>]*>([^<]*)</wsse:Password>"
                          rb"<wsse:Nonce[^>]*>([^<]*)</wsse:Nonce><wsu:Created>([^<]*)</wsu:Created>", body)
            if not m:
                return False
            import base64

            user, digest, nonce, created = (x.decode() for x in m.groups())
            ok = (user == self.user and created == self.camera_time
                  and digest == password_digest(base64.b64decode(nonce), created, self.password))
            if ok:
                self.signed_in.append("ws")
            return ok
        got = headers.get("Authorization", "")
        if not got.startswith("Digest "):
            return False
        f = {m[1]: (m[2] if m[2] is not None else m[3]) for m in onvif._CHALLENGE.finditer(got[7:])}
        challenge = {"realm": "onvif", "qop": "auth", "nonce": f["nonce"], "opaque": "0paque", "algorithm": f["algorithm"]}
        want = digest_authorization(challenge, self.user, self.password, "POST", f["uri"], cnonce=f["cnonce"],
                                    nc=int(f["nc"], 16))
        ok = (f["username"] == self.user and f["nonce"] == "n0nce" + f["algorithm"]
              and f["uri"] == "/" + url.split("/", 3)[3] and f'response="{f["response"]}"' in want)
        if ok:
            self.signed_in.append(f["algorithm"])
        return ok


# ---------------------------------------------------------------------- signing in
def test_http_digest_matches_the_examples_in_the_standard() -> None:
    """RFC 7616, section 3.9.1."""
    base = {"realm": "http-auth@example.org", "qop": "auth, auth-int", "opaque": "FQhe/qaU925kfnzjCev0ciny7QMkPqMAFRtzCUYo5tdS",
            "nonce": "7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v"}
    for algorithm, response in (("SHA-256", "753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1"),
                                ("MD5", "8ca523f5e9506fed4657c9700eebdbec")):
        got = digest_authorization({**base, "algorithm": algorithm}, "Mufasa", "Circle of Life", "GET", "/dir/index.html",
                                   cnonce="f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ")
        assert f'response="{response}"' in got and f"algorithm={algorithm}" in got
        assert 'username="Mufasa"' in got and "qop=auth, nc=00000001" in got and "Circle of Life" not in got
    # no qop offered: the older form of the answer
    old = digest_authorization({"realm": "r", "nonce": "n"}, "u", "p", "POST", "/x")
    assert "qop" not in old and "algorithm=MD5" in old and re.search(r'response="[0-9a-f]{32}"', old)


def test_the_stronger_challenge_is_answered() -> None:
    one = ['Digest realm="onvif", qop="auth", nonce="a1", algorithm=MD5, Digest realm="onvif", qop="auth", '
           'nonce="b2", algorithm=SHA-256']
    two = ['Digest realm="onvif", nonce="a1", algorithm=MD5', 'Basic realm="x"', 'Digest realm="onvif", nonce="b2", '
           'algorithm="SHA-256", qop="auth"']
    for headers in (one, two):
        found = parse_challenges(headers)
        assert [c["nonce"] for c in found] == ["a1", "b2"]
        assert pick_challenge(found)["nonce"] == "b2"
    assert pick_challenge(parse_challenges(['Digest realm="x", nonce="n"']))["nonce"] == "n"        # none named: MD5
    assert pick_challenge(parse_challenges(['Digest realm="x", nonce="n", algorithm=SHA-512-256'])) is None
    assert pick_challenge(parse_challenges(['Basic realm="x"'])) is None


def test_ws_password_digest_is_sha1_over_nonce_time_and_password() -> None:
    import base64
    import hashlib

    nonce, created = bytes(range(16)), "2026-11-19T04:40:02Z"
    want = base64.b64encode(hashlib.sha1(nonce + created.encode() + b"pw").digest()).decode()
    assert password_digest(nonce, created, "pw") == want and len(want) == 28
    head = onvif.security_header("viewer", "pw", NOW + 2, nonce)
    assert f">{want}<" in head and "<wsu:Created>2026-11-19T04:40:02Z</wsu:Created>" in head and "pw<" not in head


# ---------------------------------------------------------------------- only questions
def test_only_questions_can_be_sent() -> None:
    assert all(name.startswith("Get") for _service, name in QUESTIONS)
    for bad in ("SetSystemDateAndTime", "SystemReboot", "SetVideoEncoderConfiguration", "SetImagingSettings",
                "CreateUsers", "SetSystemFactoryDefault"):
        with pytest.raises(ValueError, match="not one of the questions"):
            envelope("device", bad)
    assert b"<q:GetProfiles " in envelope("media", "GetProfiles")


def test_an_answer_that_is_not_plain_onvif_is_refused() -> None:
    assert onvif.text(parse_answer(wrap("<tds:X><tds:Model>M</tds:Model></tds:X>")), "Model") == "M"
    for data, match in ((b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>', "document type"),
                        (b"<x>" + b"a" * (onvif.MAX_ANSWER_BYTES + 1) + b"</x>", "too large"),
                        (b"<html><body>404", "not XML"), (fault("Sender not authorized"), "Sender not authorized")):
        with pytest.raises(OnvifError, match=match):
            parse_answer(data)


# ---------------------------------------------------------------------- the report
def test_the_report_says_what_the_camera_says_about_itself() -> None:
    cam = PretendCamera("digest")
    code, report = probe("192.168.0.10", "viewer", "s3cret-pw", opener=cam, version="0.3.22", now=lambda: NOW)
    assert code == 0, report
    top, raw = report.split(onvif.RAW_STARTS, 1)
    assert top.startswith("ONVIF PROBE of 192.168.0.10 (Sentinel 0.3.22), 2026-11-19 04:40 UTC\n"
                          "Read-only: the camera was only asked questions. Nothing on it was changed.")
    assert "Where ONVIF was looked for: port 80: answered" in top
    assert "Camera clock: 2026-11-19 04:40:02 UTC (2 s ahead of this computer)" in top
    assert "Signed in as: viewer, by HTTP digest SHA-256" in top
    assert "Device: Manufacturer KT&C, Model KNC-HMSS347, FirmwareVersion 1.2.3, SerialNumber SN123, HardwareId HW1" in top
    assert "Says it conforms to: Profile S, Profile T" in top
    assert "events (http://192.168.0.10/onvif/event_service)" in top and "media (" in top and "imaging (" in top
    assert ("  1. Stream1 [prof0]: H264 1920x1080, up to 30 pictures/s, up to 4000 kbit/s, key frame every 30 pictures; "
            "sound: G711 8 kHz, 64 kbit/s\n     address: rtsp://192.168.0.10:554/stream0") in top
    assert "  2. Stream2 [prof1]: H264 1280x720, up to 30 pictures/s, up to 1000 kbit/s, key frame every 30 pictures; no sound\n" in top
    assert "  motion: RuleEngine/CellMotionDetector/Motion, VideoSource/MotionAlarm\n" in top
    assert "  tamper or picture trouble: RuleEngine/TamperDetector/Tamper, VideoSource/GlobalSceneChange/ImagingService\n" in top
    assert "  all 5: " in top and "Device/Trigger/DigitalInput" in top
    assert "  Brightness: 50; IrCutFilter: AUTO; Exposure: Mode AUTO, Gain 10" in top
    assert "WHAT DID NOT WORK: GetCapabilities: the camera refused: Optional Action Not Implemented" in top
    assert top.rstrip().endswith("RESULT: the camera answers as an ONVIF device on port 80.")
    # the camera's own answers are attached, without the account a camera may put into a stream address
    assert "THE CAMERA'S ANSWERS, AS RECEIVED" in raw and "### media GetProfiles" in raw and "<tt:GovLength>30" in raw
    assert "s3cret-pw" not in report and "viewer:" not in report and "rtsp://192.168.0.10:554/stream1" in raw
    # it asked the right service each time, and signed in with the stronger method the camera offered
    assert ("http://192.168.0.10/onvif/media_service", "GetProfiles") in cam.asked
    assert ("http://192.168.0.10/onvif/event_service", "GetEventProperties") in cam.asked
    assert set(cam.signed_in) == {"SHA-256"}
    assert all(b"s3cret-pw" not in b for b in cam.bodies)        # the password itself is never sent


@pytest.mark.parametrize(("auth", "offers", "how"), [("digest", ("MD5",), "HTTP digest MD5"),
                                                    ("ws", (), "WS-UsernameToken"), ("none", (), "")])
def test_whichever_way_the_camera_wants_to_be_signed_in_to(auth: str, offers: tuple[str, ...], how: str) -> None:
    cam = PretendCamera(auth, offers, hour=5)                    # its clock is an hour ahead of this computer
    code, report = probe("192.168.0.10", "viewer", "s3cret-pw", opener=cam, now=lambda: NOW)
    assert code == 0, report
    assert "(3602 s ahead of this computer)" in report           # ...so the sign-in uses the camera's time
    assert f"Signed in as: viewer, by {how}" in report if how else "Signed in as: viewer" in report
    assert "Model KNC-HMSS347" in report and "rtsp://192.168.0.10:554/stream0" in report


def test_a_wrong_account_and_a_camera_without_onvif_are_said_plainly() -> None:
    cam = PretendCamera("digest")
    code, report = probe("192.168.0.10", "viewer", "wrong", opener=cam, now=lambda: NOW)
    assert code == 5
    assert "RESULT: the camera speaks ONVIF on port 80, but would not say more: the camera did not accept the account" in report
    assert "Account used: 'viewer'" in report and "look for an ONVIF user" in report and "wrong" not in report.split("RESULT")[1]

    def nobody(url: str, *_a: Any) -> tuple[int, Any, bytes]:
        raise ConnectionRefusedError(10061, "refused")

    code, report = probe("192.168.0.10", "viewer", "x", ports=(80, 8899), opener=nobody, now=lambda: NOW)
    assert code == 4 and "port 80: no answer (ConnectionRefusedError" in report and "port 8899: no answer" in report
    assert "RESULT: no ONVIF answer." in report and "--port" in report

    def web_page(url: str, *_a: Any) -> tuple[int, Any, bytes]:
        return 404, Headers([]), b"<html><body>Not found</body></html>"

    code, report = probe("192.168.0.10", ports=(80,), opener=web_page, now=lambda: NOW)
    assert code == 4 and "port 80: the camera answered HTTP 404" in report


# ---------------------------------------------------------------------- over real HTTP
def test_over_real_http_with_digest_sign_in(tmp_path: Path) -> None:
    box: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            status, headers, data = box["cam"](f"http://127.0.0.1:{box['port']}{self.path}", body,
                                               {"Authorization": self.headers.get("Authorization", "")}, 5.0)
            self.send_response(status)
            for k, v in headers.items:
                self.send_header(k, v)
            self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_a: Any) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    box["port"] = srv.server_address[1]
    box["cam"] = PretendCamera("digest", base=f"http://127.0.0.1:{box['port']}")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, report = probe("127.0.0.1", "viewer", "s3cret-pw", ports=(box["port"],))
    finally:
        srv.shutdown()
        srv.server_close()
    assert code == 0, report
    assert "Signed in as: viewer, by HTTP digest SHA-256" in report and "Model KNC-HMSS347" in report
    assert "up to 4000 kbit/s" in report and "RuleEngine/CellMotionDetector/Motion" in report
    assert set(box["cam"].signed_in) == {"SHA-256"} and "s3cret-pw" not in report


# ---------------------------------------------------------------------- the command
def test_the_command_shows_the_report_and_saves_it(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, url="rtsp://192.168.0.10:554/stream0")
    cam = PretendCamera("none")
    said: list[str] = []
    assert run_probe(cfg, out=said.append, opener=cam) == 0
    text = "\n".join(said)
    assert said[0] == "Asking the camera at 192.168.0.10 (a few seconds; nothing on it is changed)..."
    assert "Model KNC-HMSS347" in text and "THE CAMERA'S ANSWERS" not in text      # the screen shows the summary
    saved = list((tmp_path / "logs").glob("onvif-probe-*.txt"))
    assert len(saved) == 1 and str(saved[0]) in text and "It holds no password." in text
    body = saved[0].read_bytes()
    assert b"THE CAMERA'S ANSWERS, AS RECEIVED" in body and b"\r\n" in body and b"<tt:GovLength>30" in body

    # another account: its password is asked for, used, and neither shown nor saved
    asked: list[str] = []
    cam = PretendCamera("ws", user="onvif-user", password="0ther-pw")
    out_file = tmp_path / "report.txt"
    code = run_probe(cfg, user="onvif-user", out_file=str(out_file), out=said.append, opener=cam,
                     ask_password=lambda prompt: asked.append(prompt) or "0ther-pw")
    assert code == 0 and asked == ["Password of the camera account 'onvif-user' (not shown): "]
    assert "Signed in as: onvif-user, by WS-UsernameToken" in out_file.read_text("utf-8")
    assert "0ther-pw" not in out_file.read_text("utf-8") and "0ther-pw" not in "\n".join(said)


def test_the_command_needs_a_camera_and_is_in_the_program(tmp_path: Path) -> None:
    said: list[str] = []
    cfg = make_config(tmp_path)
    later = dataclasses.replace(cfg, camera=dataclasses.replace(cfg.camera, url="", pending=True))
    assert run_probe(later, out=said.append) == 2 and "No camera has been added yet" in said[0]
    args = build_parser().parse_args(["onvif-probe", "--port", "8899", "--user", "onvif"])
    assert args.cmd == "onvif-probe" and args.port == 8899 and args.user == "onvif" and args.host is None
