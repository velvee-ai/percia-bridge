"""Minimal ONVIF client (Profile S/T, video only): WS-Discovery, SOAP with WS-Security digest, profiles and stream URIs.

Hand-written on the standard library + httpx. We never print or log credentials; RTSP URIs are returned without them
and credentials are only added in memory when handing the URL to ffmpeg.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse, urlunparse

import httpx

from .logs import register_secret

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tr2": "http://www.onvif.org/ver20/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}
WSD_ADDR, WSD_PORT = "239.255.255.250", 3702


class OnvifError(Exception):
    pass


class OnvifAuthError(OnvifError):
    pass


# ---------- XML helpers ----------
def parse_xml(text: str | bytes) -> ET.Element:
    raw = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
    if "<!DOCTYPE" in raw or "<!ENTITY" in raw:  # never expand entities from a device
        raise OnvifError("refusing XML with a DTD/entity declaration")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as e:
        raise OnvifError(f"invalid XML from device: {e}") from None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def iter_local(el: ET.Element, name: str):
    return (e for e in el.iter() if _local(e.tag) == name)


def first_text(el: ET.Element, name: str) -> str:
    for e in iter_local(el, name):
        if e.text and e.text.strip():
            return e.text.strip()
    return ""


def child_local(el: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in el if _local(c.tag) == name), None)


def raise_if_fault(root: ET.Element) -> None:
    fault = next(iter_local(root, "Fault"), None)
    if fault is None:
        return
    text = " ".join(t.strip() for t in fault.itertext() if t.strip())
    if re.search(r"(?i)notauthorized|sender not authorized|authentication|unauthorized", text):
        raise OnvifAuthError("the camera rejected the ONVIF username/password")
    raise OnvifError(f"ONVIF fault: {text[:200]}")


# ---------- WS-Discovery ----------
@dataclass
class Device:
    xaddrs: list[str]
    scopes: list[str] = field(default_factory=list)
    uuid: str = ""

    @property
    def xaddr(self) -> str:
        v4 = [x for x in self.xaddrs if not re.match(r"https?://\[", x)]
        return (v4 or self.xaddrs)[0]

    @property
    def host(self) -> str:
        return urlparse(self.xaddr).hostname or ""

    @property
    def port(self) -> int:
        u = urlparse(self.xaddr)
        return u.port or (443 if u.scheme == "https" else 80)

    def _scope(self, key: str) -> str:
        for s in self.scopes:
            m = re.match(rf"(?i)onvif://www\.onvif\.org/{key}/(.*)", s)
            if m:
                return unquote(m.group(1))
        return ""

    @property
    def name(self) -> str:
        return self._scope("name")

    @property
    def hardware(self) -> str:
        return self._scope("hardware")

    @property
    def profiles(self) -> list[str]:
        return sorted({unquote(s.rsplit("/", 1)[-1]) for s in self.scopes if re.search(r"(?i)onvif\.org/Profile/", s)})


def probe_message(msg_id: str | None = None) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f"<e:Header><w:MessageID>uuid:{msg_id or uuid.uuid4()}</w:MessageID><w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
        "<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>"
        "<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"
    ).encode()


def parse_probe_matches(data: bytes | str) -> list[Device]:
    root = parse_xml(data)
    out = []
    for m in iter_local(root, "ProbeMatch"):
        xaddrs = first_text(m, "XAddrs").split()
        xaddrs = [x for x in xaddrs if re.match(r"(?i)^https?://", x)]
        if not xaddrs:
            continue
        out.append(Device(xaddrs=xaddrs, scopes=first_text(m, "Scopes").split(), uuid=first_text(m, "Address")))
    return out


def _discover_blocking(timeout: float, bind_ip: str | None) -> list[Device]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)  # stays on the local network
        if bind_ip:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind_ip))
            sock.bind((bind_ip, 0))
        sock.settimeout(0.5)
        for _ in range(2):
            sock.sendto(probe_message(), (WSD_ADDR, WSD_PORT))
        found: dict[str, Device] = {}
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                for d in parse_probe_matches(data):
                    found.setdefault(d.uuid or d.xaddr, d)
            except OnvifError:
                continue
        return list(found.values())
    finally:
        sock.close()


async def discover(timeout: float = 3.0, bind_ip: str | None = None) -> list[Device]:
    """WS-Discovery multicast probe on the local network only (TTL 2). Does not scan address ranges."""
    return await asyncio.get_running_loop().run_in_executor(None, _discover_blocking, timeout, bind_ip)


# ---------- SOAP ----------
def security_header(username: str, password: str, now: datetime, nonce: bytes | None = None) -> str:
    nonce = nonce or os.urandom(16)
    created = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (
        '<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        f'<UsernameToken><Username>{_esc(username)}</Username>'
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
        f'{digest}</Password><Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f'{base64.b64encode(nonce).decode()}</Nonce>'
        f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
        "</UsernameToken></Security>"
    )


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def envelope(body: str, header: str = "") -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            f'xmlns:tds="{NS["tds"]}" xmlns:trt="{NS["trt"]}" xmlns:tr2="{NS["tr2"]}" xmlns:tt="{NS["tt"]}">'
            f"<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>")


def parse_system_time(text: str) -> datetime:
    root = parse_xml(text)
    utc = next(iter_local(root, "UTCDateTime"), None)
    if utc is None:
        raise OnvifError("no UTCDateTime in GetSystemDateAndTime")
    g = lambda n: int(first_text(utc, n))  # noqa: E731
    return datetime(g("Year"), g("Month"), g("Day"), g("Hour"), g("Minute"), g("Second"), tzinfo=timezone.utc)


def parse_capabilities_media_xaddr(text: str) -> str:
    root = parse_xml(text)
    raise_if_fault(root)
    media = next(iter_local(root, "Media"), None)
    x = first_text(media, "XAddr") if media is not None else ""
    if not x:
        raise OnvifError("device reports no Media service")
    return x


def parse_services(text: str) -> dict[str, str]:
    """GetServices -> {namespace: xaddr}."""
    root = parse_xml(text)
    raise_if_fault(root)
    out = {}
    for svc in iter_local(root, "Service"):
        ns, x = first_text(svc, "Namespace"), first_text(svc, "XAddr")
        if ns and x:
            out[ns] = x
    return out


@dataclass
class StreamProfile:
    token: str
    name: str
    encoding: str = ""
    width: int = 0
    height: int = 0
    uri: str = ""

    def label(self) -> str:
        return f"{self.name or self.token} {self.encoding or '?'} {self.width}x{self.height}".strip()


def parse_profiles(text: str) -> list[StreamProfile]:
    """Handles Media1 (trt:Profiles/tt:VideoEncoderConfiguration) and Media2 (tr2:Profiles/tr2:Configurations/tr2:VideoEncoder)."""
    root = parse_xml(text)
    raise_if_fault(root)
    out = []
    for p in iter_local(root, "Profiles"):
        token = p.get("token", "")
        nm = child_local(p, "Name")
        name = (nm.text or "").strip() if nm is not None and nm.text else p.get("Name", "")
        enc_el = next(iter_local(p, "VideoEncoderConfiguration"), None)
        if enc_el is None:
            enc_el = next(iter_local(p, "VideoEncoder"), None)
        if enc_el is None:
            continue  # audio-only / metadata-only profile
        res = next(iter_local(enc_el, "Resolution"), None)
        out.append(StreamProfile(
            token=token, name=name, encoding=first_text(enc_el, "Encoding").upper().replace(".", ""),
            width=int(first_text(res, "Width") or 0) if res is not None else 0,
            height=int(first_text(res, "Height") or 0) if res is not None else 0))
    return out


def parse_stream_uri(text: str) -> str:
    root = parse_xml(text)
    raise_if_fault(root)
    uri = first_text(root, "Uri")
    if not uri:
        raise OnvifError("GetStreamUri returned no URI")
    return uri


def choose_profile(profiles: list[StreamProfile], wanted: str = "", prefer_height: int = 720) -> StreamProfile:
    """Explicit name/token wins. Otherwise: an H.264 profile (copy mode, cheapest), the one whose height is the largest
    that is <= prefer_height (else the smallest), falling back to any video profile."""
    if not profiles:
        raise OnvifError("the camera has no video profiles")
    if wanted:
        for p in profiles:
            if wanted in (p.token, p.name):
                return p
        raise OnvifError(f"no profile named '{wanted}' (have: {', '.join(p.name or p.token for p in profiles)})")
    pool = [p for p in profiles if p.encoding == "H264"] or profiles
    fits = [p for p in pool if p.height and p.height <= prefer_height]
    return max(fits, key=lambda p: p.height) if fits else min(pool, key=lambda p: p.height or 10**9)


def with_credentials(uri: str, username: str, password: str) -> str:
    """Inject user:pass into an RTSP URI (in memory only; never print the result)."""
    if not username:
        return uri
    u = urlparse(uri)
    host = u.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{host}" + (f":{u.port}" if u.port else "")
    return urlunparse(u._replace(netloc=netloc))


def fix_host(uri: str, device_host: str) -> str:
    """Some cameras answer with a 127.0.0.1/0.0.0.0/other-subnet address in the URI; use the address we reached them at."""
    u = urlparse(uri)
    if u.hostname in ("127.0.0.1", "0.0.0.0", "localhost", None):
        netloc = device_host + (f":{u.port}" if u.port else "")
        return urlunparse(u._replace(netloc=netloc))
    return uri


class OnvifClient:
    def __init__(self, host: str, port: int = 80, username: str = "", password: str = "", http: httpx.AsyncClient | None = None,
                 scheme: str = "http", clock_offset: timedelta | None = None):
        self.host, self.port, self.username, self.password = host, port, username, password
        self.scheme = scheme
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(8.0))
        self.offset = clock_offset
        register_secret(password)

    @property
    def device_url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}/onvif/device_service"

    async def _post(self, url: str, body: str, authed: bool = True) -> str:
        header = ""
        if authed and self.username:
            now = datetime.now(timezone.utc) + (self.offset or timedelta())
            header = security_header(self.username, self.password, now)
        try:
            r = await self.http.post(url, content=envelope(body, header).encode(), headers={"content-type": "application/soap+xml; charset=utf-8"})
        except httpx.HTTPError as e:
            raise OnvifError(f"cannot reach the camera at {self.host}:{self.port}: {type(e).__name__}") from None
        if r.status_code in (401, 403):
            raise OnvifAuthError("the camera rejected the ONVIF username/password")
        if r.status_code >= 500 and "Fault" not in r.text:
            raise OnvifError(f"camera answered HTTP {r.status_code}")
        return r.text

    async def device_identity(self) -> str:
        """A stable id for this physical device (serial number + model), or "" when the camera will not say.
        GetDeviceInformation usually needs the login; a camera that refuses just means we fall back to host:port."""
        try:
            if self.offset is None:
                await self.sync_clock()
            text = await self._post(self.device_url, "<tds:GetDeviceInformation/>")
            root = parse_xml(text)
            raise_if_fault(root)
            parts = [first_text(root, n) for n in ("Manufacturer", "Model", "SerialNumber", "HardwareId")]
            return "|".join(parts) if parts[2] or parts[3] else ""
        except OnvifError:
            return ""

    async def sync_clock(self) -> None:
        try:
            text = await self._post(self.device_url, "<tds:GetSystemDateAndTime/>", authed=False)
            self.offset = parse_system_time(text) - datetime.now(timezone.utc)
        except OnvifError:
            self.offset = timedelta()

    async def media_endpoints(self) -> tuple[str | None, str | None]:
        """(media1 xaddr, media2 xaddr) via GetServices, falling back to GetCapabilities for media1."""
        m1 = m2 = None
        try:
            svc = parse_services(await self._post(self.device_url, "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>"))
            m1, m2 = svc.get(NS["trt"]), svc.get(NS["tr2"])
        except OnvifAuthError:
            raise
        except OnvifError:
            pass
        if not m1 and not m2:
            m1 = parse_capabilities_media_xaddr(await self._post(
                self.device_url, "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>"))
        return m1, m2

    async def list_profiles(self) -> list[StreamProfile]:
        """Profiles with RTSP URIs (credentials NOT included). Media2 first (it reports H.265), then Media1."""
        if self.offset is None:
            await self.sync_clock()
        m1, m2 = await self.media_endpoints()
        profiles: list[StreamProfile] = []
        if m2:
            try:
                profiles = parse_profiles(await self._post(m2, "<tr2:GetProfiles><tr2:Type>All</tr2:Type></tr2:GetProfiles>"))
                for p in profiles:
                    p.uri = parse_stream_uri(await self._post(
                        m2, f"<tr2:GetStreamUri><tr2:Protocol>RTSP</tr2:Protocol><tr2:ProfileToken>{_esc(p.token)}</tr2:ProfileToken></tr2:GetStreamUri>"))
            except OnvifAuthError:
                raise
            except OnvifError:
                profiles = []
        if not profiles and m1:
            profiles = parse_profiles(await self._post(m1, "<trt:GetProfiles/>"))
            for p in profiles:
                p.uri = parse_stream_uri(await self._post(
                    m1, "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol>"
                        f"</tt:Transport></trt:StreamSetup><trt:ProfileToken>{_esc(p.token)}</trt:ProfileToken></trt:GetStreamUri>"))
        for p in profiles:
            p.uri = fix_host(p.uri, self.host)
        return profiles

    async def stream_url(self, wanted_profile: str = "") -> str:
        """RTSP URL with credentials for ffmpeg (in memory only)."""
        profiles = await self.list_profiles()
        chosen = choose_profile(profiles, wanted_profile)
        url = with_credentials(chosen.uri, self.username, self.password)
        register_secret(url)
        return url

    async def aclose(self) -> None:
        await self.http.aclose()
