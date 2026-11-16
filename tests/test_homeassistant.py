import asyncio
import json
from pathlib import Path

import httpx
import pytest
import websockets

from percia_bridge import homeassistant as ha
from percia_bridge.config import NAME_RE

FX = Path(__file__).parent / "fixtures"
STATES = json.loads((FX / "ha_states.json").read_text())


def test_parse_states_only_cameras_sorted():
    cams = ha.parse_states(STATES)
    assert [c.entity_id for c in cams] == ["camera.backyard", "camera.front_door", "camera.old", "camera.日本"]
    by = {c.entity_id: c for c in cams}
    assert not by["camera.old"].available and by["camera.old"].name == "Old"
    assert by["camera.backyard"].available and "light.kitchen" not in by


def test_percia_name_is_valid():
    assert ha.percia_name("Front Door (Reolink)", "camera.front_door") == "Front Door Reolink"
    assert ha.percia_name("玄関", "camera.genkan") == "genkan"
    assert ha.percia_name("", "camera.a-b c") == "a-b_c"
    for friendly in ("x" * 100, "___", "(((", "Ünïcode cam", "-lead"):
        assert NAME_RE.match(ha.percia_name(friendly, "camera.test"))


def test_list_cameras_via_supervisor_proxy():
    def handler(req):
        assert req.url.path == "/core/api/states" and req.headers["authorization"] == "Bearer SUPTOK"
        return httpx.Response(200, json=STATES)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert len(asyncio.run(ha.list_cameras("SUPTOK", "http://supervisor/core", http))) == 4
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(ha.HAError, match="homeassistant_api"):
        asyncio.run(ha.list_cameras("x", http=http))


def test_parse_stream_result():
    assert ha.parse_stream_result({"success": True, "result": {"url": "/api/hls/abc/master_playlist.m3u8"}}).startswith("/api/hls/")
    with pytest.raises(ha.HAError, match="no stream source"):
        ha.parse_stream_result({"success": False, "error": {"code": "unknown_error", "message": "does not support stream"}})
    with pytest.raises(ha.HAError):
        ha.parse_stream_result({"success": True, "result": {"url": "http://evil/x"}})


def test_resolve_order_override_hls_proxy():
    async def hls_ok(eid, tok, ws):
        return "/api/hls/t/master_playlist.m3u8"

    async def hls_fail(eid, tok, ws):
        raise ha.HAError("no")
    url, h, kind = asyncio.run(ha.resolve_stream("camera.a", "T", "rtsp://cam/stream", hls=hls_ok))
    assert (url, h, kind) == ("rtsp://cam/stream", {}, "rtsp")
    url, h, kind = asyncio.run(ha.resolve_stream("camera.a", "T", "", base="http://s/core", hls=hls_ok))
    assert (url, kind, h["Authorization"]) == ("http://s/core/api/hls/t/master_playlist.m3u8", "hls", "Bearer T")
    url, h, kind = asyncio.run(ha.resolve_stream("camera.a b", "T", "", base="http://s/core", hls=hls_fail))
    assert (url, kind) == ("http://s/core/api/camera_proxy_stream/camera.a%20b", "proxy")


def test_hls_stream_path_over_a_fake_websocket():
    async def go(reply):
        got = {}

        async def serve(ws):
            await ws.send(json.dumps({"type": "auth_required"}))
            got["auth"] = json.loads(await ws.recv())
            await ws.send(json.dumps({"type": "auth_ok"}))
            got["cmd"] = json.loads(await ws.recv())
            await ws.send(json.dumps({"id": 99, "type": "result", "success": True, "result": {}}))  # unrelated message first
            await ws.send(json.dumps({"id": 1, "type": "result", **reply}))
        async with websockets.serve(serve, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            try:
                return await ha.hls_stream_path("camera.front", "SUPTOK", f"ws://127.0.0.1:{port}"), got
            except ha.HAError as e:
                return e, got
    path, got = asyncio.run(go({"success": True, "result": {"url": "/api/hls/zz/master_playlist.m3u8"}}))
    assert path == "/api/hls/zz/master_playlist.m3u8"
    assert got["auth"] == {"type": "auth", "access_token": "SUPTOK"}
    assert got["cmd"]["type"] == "camera/stream" and got["cmd"]["entity_id"] == "camera.front"
    err, _ = asyncio.run(go({"success": False, "error": {"message": "no stream"}}))
    assert isinstance(err, ha.HAError)
