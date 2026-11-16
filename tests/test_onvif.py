import asyncio
import base64
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from percia_bridge import onvif
from percia_bridge.logs import redact

FX = Path(__file__).parent / "fixtures" / "onvif"
fx = lambda n: (FX / n).read_text()  # noqa: E731


def test_parse_probe_match():
    (d,) = onvif.parse_probe_matches(fx("probe_match.xml"))
    assert d.host == "192.168.1.64" and d.port == 80  # IPv4 XAddr preferred over IPv6
    assert d.name == "Front Door" and d.hardware == "DS-2CD2143G2"
    assert d.profiles == ["Streaming", "T"]
    assert d.uuid.startswith("urn:uuid:3fa1fe68")


def test_probe_message_is_valid_xml_for_network_video_transmitter():
    root = ET.fromstring(onvif.probe_message("abc"))
    assert "NetworkVideoTransmitter" in ET.tostring(root, encoding="unicode")


def test_parse_system_time_and_capabilities_and_services():
    assert onvif.parse_system_time(fx("system_date_time.xml")) == datetime(2026, 11, 8, 13, 5, 9, tzinfo=timezone.utc)
    assert onvif.parse_capabilities_media_xaddr(fx("capabilities.xml")) == "http://192.168.1.64/onvif/Media"
    svc = onvif.parse_services(fx("services.xml"))
    assert svc[onvif.NS["tr2"]].endswith("/Media2") and svc[onvif.NS["trt"]].endswith("/Media")


def test_parse_profiles_media1_skips_audio_only():
    ps = onvif.parse_profiles(fx("profiles_media1.xml"))
    assert [(p.token, p.name, p.encoding, p.width, p.height) for p in ps] == [
        ("Profile_1", "mainStream", "H264", 2560, 1440), ("Profile_2", "subStream", "H264", 640, 360), ("Profile_3", "jpegStream", "JPEG", 704, 576)]


def test_parse_profiles_media2_h265():
    ps = onvif.parse_profiles(fx("profiles_media2.xml"))
    assert [(p.token, p.encoding, p.height) for p in ps] == [("profile_h265", "H265", 2160), ("profile_sub", "H264", 720)]


def test_stream_uri_and_faults():
    assert onvif.parse_stream_uri(fx("stream_uri_media1.xml")) == "rtsp://192.168.1.64:554/Streaming/Channels/102"
    assert onvif.parse_stream_uri(fx("stream_uri_media2.xml")) == "rtsp://192.168.1.64:554/media/video1"
    with pytest.raises(onvif.OnvifAuthError):
        onvif.parse_profiles(fx("fault_notauthorized.xml"))


def test_refuses_dtd():
    with pytest.raises(onvif.OnvifError, match="DTD"):
        onvif.parse_xml(fx("evil_entity.xml"))


def test_choose_profile():
    ps = onvif.parse_profiles(fx("profiles_media1.xml"))
    assert onvif.choose_profile(ps).token == "Profile_2"  # H.264, largest <= 720p
    assert onvif.choose_profile(ps, "mainStream").token == "Profile_1"
    assert onvif.choose_profile(ps, "Profile_3").token == "Profile_3"
    with pytest.raises(onvif.OnvifError, match="no profile named"):
        onvif.choose_profile(ps, "zzz")
    h265 = onvif.parse_profiles(fx("profiles_media2_h265_only.xml"))
    assert onvif.choose_profile(h265).token == "p1"  # nothing better; will be transcoded
    big_only = [onvif.StreamProfile("a", "a", "H264", 3840, 2160), onvif.StreamProfile("b", "b", "H264", 1920, 1080)]
    assert onvif.choose_profile(big_only).token == "b"  # none fits: smallest H.264


def test_credentials_helpers():
    u = onvif.with_credentials("rtsp://192.168.1.64:554/s", "ad min", "p@ss:w/rd")
    assert u == "rtsp://ad%20min:p%40ss%3Aw%2Frd@192.168.1.64:554/s"
    assert onvif.with_credentials("rtsp://h/s", "", "") == "rtsp://h/s"
    assert onvif.fix_host("rtsp://127.0.0.1:554/x", "192.168.1.9") == "rtsp://192.168.1.9:554/x"
    assert onvif.fix_host("rtsp://192.168.1.64/x", "192.168.1.9") == "rtsp://192.168.1.64/x"


def test_ws_security_digest_is_correct():
    now = datetime(2026, 11, 8, 13, 5, 9, tzinfo=timezone.utc)
    nonce = b"0123456789abcdef"
    xml = onvif.security_header("admin", "pw<&>", now, nonce)
    ET.fromstring(f'<r xmlns:s="http://www.w3.org/2003/05/soap-envelope">{xml}</r>')
    digest = re.search(r"PasswordDigest\">([^<]+)<", xml).group(1)
    assert digest == base64.b64encode(hashlib.sha1(nonce + b"2026-11-08T13:05:09Z" + b"pw<&>").digest()).decode()
    assert "pw" not in xml  # the password itself is never sent


def make_client(handler, **kw):
    return onvif.OnvifClient("192.168.1.64", 80, "admin", "s3cretpw", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kw)


def device(responses, seen=None):
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode()
        if seen is not None:
            seen.append((str(req.url), body))
        for needle, resp in responses:
            if needle in body:
                return httpx.Response(200 if "Fault" not in resp else 400, text=resp)
        return httpx.Response(500, text="<x/>")
    return handler


def test_list_profiles_prefers_media2_and_sends_digest():
    seen = []
    c = make_client(device([
        ("GetSystemDateAndTime", fx("system_date_time.xml")), ("GetServices", fx("services.xml")),
        ("tr2:GetProfiles", fx("profiles_media2.xml")), ("tr2:GetStreamUri", fx("stream_uri_media2.xml"))], seen))
    ps = asyncio.run(c.list_profiles())
    assert [p.encoding for p in ps] == ["H265", "H264"] and ps[0].uri == "rtsp://192.168.1.64:554/media/video1"
    assert any("/onvif/Media2" in u for u, _ in seen)
    authed = [b for _, b in seen if "GetServices" in b]
    assert "PasswordDigest" in authed[0] and "s3cretpw" not in "".join(b for _, b in seen)
    # the clock offset from the camera is used for the digest timestamp
    assert "2026-11-08T13:05:" in authed[0]


def test_list_profiles_falls_back_to_media1_via_capabilities():
    c = make_client(device([
        ("GetSystemDateAndTime", fx("system_date_time.xml")), ("GetServices", "<x/>"), ("GetCapabilities", fx("capabilities.xml")),
        ("trt:GetProfiles", fx("profiles_media1.xml")), ("trt:GetStreamUri", fx("stream_uri_media1.xml"))]))
    ps = asyncio.run(c.list_profiles())
    assert [p.name for p in ps] == ["mainStream", "subStream", "jpegStream"]
    assert all(p.uri.startswith("rtsp://192.168.1.64:554/") for p in ps)


def test_media2_failure_falls_back_to_media1():
    c = make_client(device([
        ("GetSystemDateAndTime", fx("system_date_time.xml")), ("GetServices", fx("services.xml")),
        ("tr2:GetProfiles", fx("fault_notauthorized.xml").replace("NotAuthorized", "ActionNotSupported").replace("Sender not Authorized", "nope")),
        ("trt:GetProfiles", fx("profiles_media1.xml")), ("trt:GetStreamUri", fx("stream_uri_media1.xml"))]))
    assert len(asyncio.run(c.list_profiles())) == 3


def test_wrong_password_is_a_clear_error():
    def handler(req):
        return httpx.Response(200, text=fx("system_date_time.xml")) if "GetSystemDateAndTime" in req.content.decode() else httpx.Response(401, text="")
    with pytest.raises(onvif.OnvifAuthError, match="username/password"):
        asyncio.run(make_client(handler).list_profiles())


def test_stream_url_has_credentials_but_they_are_redacted_in_logs():
    c = make_client(device([
        ("GetSystemDateAndTime", fx("system_date_time.xml")), ("GetServices", fx("services_media1_only.xml")),
        ("trt:GetProfiles", fx("profiles_media1.xml")), ("trt:GetStreamUri", fx("stream_uri_localhost.xml"))]))
    url = asyncio.run(c.stream_url("Profile_1"))
    assert url == "rtsp://admin:s3cretpw@192.168.1.64:554/live/ch1"  # 127.0.0.1 in the reply is replaced by the address we reached
    assert "s3cretpw" not in redact(f"ffmpeg input {url}")


def test_unreachable_camera():
    def handler(req):
        raise httpx.ConnectError("boom")
    with pytest.raises(onvif.OnvifError, match="cannot reach"):
        asyncio.run(make_client(handler, clock_offset=onvif.timedelta()).list_profiles())
