"""A stable id per camera, used as the `externalId` when registering with Percia.

Registering the same externalId twice returns the same Percia camera, so restarts, re-adds, a wiped state dir and a second
bridge on the same account never create duplicates. The id is a hash of the camera's own identity (never the identity itself),
so no IP address, serial number or camera name reaches the server through it.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from .config import CameraCfg
from .logs import strip_credentials

PREFIX = "pb-"
EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def _hash(kind: str, identity: str) -> str:
    return PREFIX + hashlib.sha256(f"percia-bridge:v1:{kind}:{identity}".encode()).hexdigest()[:32]


def rtsp_identity(url: str) -> str:
    """Host, port and path of the RTSP URL, lower-cased, without credentials: the same camera however the login is written."""
    u = urlparse(strip_credentials(url))
    port = u.port or 554
    return f"{(u.hostname or '').lower()}:{port}{u.path}{('?' + u.query) if u.query else ''}"


def external_id(cam: CameraCfg, onvif_device: str = "") -> str:
    """`onvif_device` is the device's serial/model string from ONVIF GetDeviceInformation when it could be read."""
    if cam.external_id:
        return cam.external_id
    s = cam.source
    if s.type == "onvif":
        if onvif_device:
            return _hash("onvif", onvif_device)
        return _hash("onvif-host", f"{s.host.lower()}:{s.port}:{s.profile}")
    if s.type == "frigate":
        return _hash("frigate", s.camera)
    if s.type == "homeassistant":
        return _hash("homeassistant", s.camera)
    return _hash("rtsp", rtsp_identity(s.url))


async def resolve_external_id(cam: CameraCfg) -> str:
    """external_id() plus, for ONVIF, a look at the device's serial number so the id survives an IP change."""
    if cam.external_id or cam.source.type != "onvif":
        return external_id(cam)
    from .onvif import OnvifClient

    s = cam.source
    client = OnvifClient(s.host, s.port, s.username, s.password)
    try:
        return external_id(cam, await client.device_identity())
    except Exception:  # noqa: BLE001 - camera unreachable: host:port is the fallback
        return external_id(cam)
    finally:
        await client.aclose()
