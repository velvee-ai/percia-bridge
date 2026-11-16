import asyncio
import json
from pathlib import Path

import httpx
import pytest

from percia_bridge import frigate

FX = Path(__file__).parent / "fixtures"


def cams():
    return {c.name: c for c in frigate.parse_config(json.loads((FX / "frigate_config.json").read_text()))}


def test_prefers_detect_role_restream():
    c = cams()
    assert c["front_door"].stream == "front_door_sub"  # detect input, not the record one
    assert c["garage"].stream == "garage"


def test_camera_without_restream_is_flagged():
    d = cams()["driveway"]
    assert d.stream is None and "go2rtc" in d.note


def test_disabled_and_undefined_stream():
    a = cams()["attic"]
    assert not a.enabled and "not defined in go2rtc.streams" in a.note


def test_config_file_yaml():
    (p,) = frigate.parse_config_file(FX / "frigate_config.yml")
    assert (p.name, p.stream) == ("porch", "porch")


def test_not_a_frigate_config():
    with pytest.raises(frigate.FrigateError):
        frigate.parse_config({"hello": 1})


def test_restream_url():
    assert frigate.restream_url("http://frigate:5000", "front_door_sub") == "rtsp://frigate:8554/front_door_sub"
    assert frigate.restream_url("http://192.168.1.5:5000", "x", 8555) == "rtsp://192.168.1.5:8555/x"
    with pytest.raises(frigate.FrigateError):
        frigate.restream_url("http://f", "a b/../c")


def test_fetch_cameras_ok_and_errors():
    body = (FX / "frigate_config.json").read_text()

    def ok(req):
        assert req.url.path == "/api/config"
        return httpx.Response(200, text=body)
    http = httpx.AsyncClient(transport=httpx.MockTransport(ok))
    assert len(asyncio.run(frigate.fetch_cameras("http://frigate:5000", http=http))) == 4
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(frigate.FrigateError, match="401"):
        asyncio.run(frigate.fetch_cameras("http://frigate:5000", http=http))

    def boom(req):
        raise httpx.ConnectError("x")
    with pytest.raises(frigate.FrigateError, match="cannot reach"):
        asyncio.run(frigate.fetch_cameras("http://frigate:5000", http=httpx.AsyncClient(transport=httpx.MockTransport(boom))))
