"""Home Assistant input (used by the add-on): list camera entities through the Supervisor proxy and resolve a stream."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from .config import NAME_RE

SUPERVISOR_CORE = "http://supervisor/core"
SUPERVISOR_WS = "ws://supervisor/core/websocket"


class HAError(Exception):
    pass


@dataclass
class HACamera:
    entity_id: str
    name: str
    state: str

    @property
    def available(self) -> bool:
        return self.state not in ("unavailable", "unknown")


def percia_name(friendly: str, entity_id: str) -> str:
    """A valid Percia camera name (1-60 chars, NAME_RE) from the entity's friendly name."""
    n = re.sub(r"[^A-Za-z0-9 _.-]+", " ", friendly or "").strip(" _.-")
    n = re.sub(r"\s+", " ", n)[:60].strip(" _.-")
    if not NAME_RE.match(n):
        n = re.sub(r"[^A-Za-z0-9_.-]+", "_", entity_id.split(".", 1)[-1])[:60] or "camera"
    return n


def parse_states(states: list[dict]) -> list[HACamera]:
    out = []
    for s in states:
        eid = s.get("entity_id", "")
        if not eid.startswith("camera."):
            continue
        attrs = s.get("attributes") or {}
        out.append(HACamera(eid, str(attrs.get("friendly_name") or eid.split(".", 1)[1].replace("_", " ").title()), str(s.get("state", ""))))
    return sorted(out, key=lambda c: c.name.lower())


async def list_cameras(token: str, base: str = SUPERVISOR_CORE, http: httpx.AsyncClient | None = None) -> list[HACamera]:
    own = http is None
    http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
    try:
        r = await http.get(f"{base}/api/states", headers={"authorization": f"Bearer {token}"})
        if r.status_code == 401:
            raise HAError("Home Assistant rejected the Supervisor token (is `homeassistant_api: true` set in the add-on?)")
        if r.status_code != 200:
            raise HAError(f"Home Assistant answered HTTP {r.status_code} listing entities")
        return parse_states(r.json())
    except httpx.HTTPError as e:
        raise HAError(f"cannot reach Home Assistant: {type(e).__name__}") from None
    finally:
        if own:
            await http.aclose()


def parse_stream_result(msg: dict) -> str:
    """Websocket answer to {type: 'camera/stream'} -> relative URL like /api/hls/<token>/master_playlist.m3u8."""
    if not msg.get("success"):
        err = (msg.get("error") or {}).get("message", "unknown error")
        raise HAError(f"no stream source for this camera ({err})")
    url = (msg.get("result") or {}).get("url", "")
    if not url.startswith("/"):
        raise HAError("Home Assistant returned an unexpected stream URL")
    return url


async def hls_stream_path(entity_id: str, token: str, ws_url: str = SUPERVISOR_WS) -> str:
    import websockets

    try:
        async with websockets.connect(ws_url, open_timeout=10, max_size=1_000_000) as ws:
            first = json.loads(await ws.recv())
            if first.get("type") != "auth_required":
                raise HAError("unexpected websocket greeting from Home Assistant")
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            if json.loads(await ws.recv()).get("type") != "auth_ok":
                raise HAError("Home Assistant websocket rejected the token")
            await ws.send(json.dumps({"id": 1, "type": "camera/stream", "entity_id": entity_id, "format": "hls"}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == 1:
                    return parse_stream_result(msg)
    except (OSError, websockets.WebSocketException) as e:
        raise HAError(f"websocket to Home Assistant failed: {type(e).__name__}") from None


async def resolve_stream(entity_id: str, token: str, rtsp_override: str = "", base: str = SUPERVISOR_CORE,
                         ws_url: str = SUPERVISOR_WS, hls=hls_stream_path) -> tuple[str, dict[str, str], str]:
    """(url, ffmpeg headers, kind). Order: user-pasted RTSP, HLS from the stream integration, camera proxy (MJPEG)."""
    auth = {"Authorization": f"Bearer {token}"}
    if rtsp_override:
        return rtsp_override, {}, "rtsp"
    try:
        path = await hls(entity_id, token, ws_url)
        return f"{base}{path}", auth, "hls"
    except HAError:
        return f"{base}/api/camera_proxy_stream/{quote(entity_id)}", auth, "proxy"
