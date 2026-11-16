import asyncio
import json
from pathlib import Path

import pytest

from percia_bridge import ha_addon as addon
from percia_bridge import homeassistant as ha
from percia_bridge.config import Video

STATES = json.loads((Path(__file__).parent / "fixtures" / "ha_states.json").read_text())
CAMS = ha.parse_states(STATES)
KNOWN = {c.entity_id for c in CAMS}


def test_update_selection_enable_and_rtsp_override():
    sel = addon.update_selection({}, {"entities": {"camera.front_door": {"enabled": True, "rtsp_url": " rtsp://u:p@10.0.0.2/s "}}}, KNOWN)
    assert sel["camera.front_door"] == {"enabled": True, "rtsp_url": "rtsp://u:p@10.0.0.2/s"}
    sel = addon.update_selection(sel, {"entities": {"camera.front_door": {"enabled": False}}}, KNOWN)  # rtsp_url absent = keep
    assert sel["camera.front_door"] == {"enabled": False, "rtsp_url": "rtsp://u:p@10.0.0.2/s"}
    sel = addon.update_selection(sel, {"entities": {"camera.front_door": {"rtsp_url": ""}}}, KNOWN)
    assert sel["camera.front_door"]["rtsp_url"] == ""


@pytest.mark.parametrize("bad", [
    {"entities": {"camera.nope": {"enabled": True}}},
    {"entities": {"camera.front_door": {"rtsp_url": "http://x"}}},
    {"entities": {"camera.front_door": {"rtsp_url": "file:///etc/passwd"}}},
    {"entities": "x"}, {},
])
def test_update_selection_rejects(bad):
    with pytest.raises(ValueError):
        addon.update_selection({}, bad, KNOWN)


def test_at_most_ten_cameras():
    known = {f"camera.c{i}" for i in range(11)}
    with pytest.raises(ValueError, match="at most 10"):
        addon.update_selection({}, {"entities": {k: {"enabled": True} for k in known}}, known)


def test_desired_cameras_only_enabled_with_unique_names():
    twin = ha.parse_states([{"entity_id": "camera.a", "state": "idle", "attributes": {"friendly_name": "Door"}},
                            {"entity_id": "camera.b", "state": "idle", "attributes": {"friendly_name": "door"}}])
    sel = {"camera.a": {"enabled": True, "rtsp_url": ""}, "camera.b": {"enabled": True, "rtsp_url": "rtsp://h/s"}}
    cams = addon.desired_cameras(twin, sel, Video())
    assert [c.name for c in cams] == ["Door", "door 2"]
    assert cams[1].source.type == "homeassistant" and cams[1].source.url == "rtsp://h/s" and cams[0].source.camera == "camera.a"
    assert addon.desired_cameras(CAMS, {}, Video()) == []


def test_ui_cameras_never_returns_stream_credentials():
    sel = {"camera.front_door": {"enabled": True, "rtsp_url": "rtsp://admin:topsecret@10.0.0.2/s"}}
    rows = addon.ui_cameras(CAMS, sel, {"Front Door Reolink": {"state": "streaming", "mode": "copy", "last_error": ""}})
    row = next(r for r in rows if r["entity_id"] == "camera.front_door")
    assert "topsecret" not in json.dumps(rows) and row["rtsp_url"] == "rtsp://10.0.0.2/s" and row["has_rtsp_url"]
    assert row["state"] == "streaming" and row["enabled"]
    old = next(r for r in rows if r["entity_id"] == "camera.old")
    assert not old["available"]


def test_selection_file_roundtrip(tmp_path):
    f = tmp_path / "sel.json"
    addon.save_selection({"camera.a": {"enabled": True, "rtsp_url": ""}}, f)
    assert addon.load_selection(f) == {"camera.a": {"enabled": True, "rtsp_url": ""}}
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    f.write_text("garbage")
    assert addon.load_selection(f) == {}


class FakeBridge:
    def __init__(self):
        self.applied = []

    async def apply(self, cams):
        self.applied.append([c.name for c in cams])

    def cameras(self):
        return []


def test_ui_server_routes_and_ingress_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(addon, "SELECTION_PATH", tmp_path / "sel.json")
    b = FakeBridge()
    a = addon.Addon(b, "T", Video())
    a.cameras, a.sel = CAMS, {}
    ing = ("172.30.32.2", 5555)

    async def go():
        assert (await a.http("GET", "/", {}, b"", ("10.0.0.5", 1)))[0] == 403  # not the ingress gateway
        status, ctype, body = await a.http("GET", "/", {}, b"", ing)
        assert status == 200 and b"Percia Bridge" in body and ctype.startswith("text/html")
        status, _, body = await a.http("GET", "/api/cameras", {}, b"", ing)
        assert status == 200 and len(json.loads(body)["cameras"]) == 4
        post = lambda payload, ct="application/json": a.http("POST", "/api/selection", {"content-type": ct}, json.dumps(payload).encode(), ing)  # noqa: E731
        assert (await post({"entities": {"camera.front_door": {"enabled": True}}}))[0] == 200
        assert b.applied[-1] == ["Front Door Reolink"]
        assert json.loads((tmp_path / "sel.json").read_text())["entities"]["camera.front_door"]["enabled"] is True
        assert (await post({"entities": {"camera.nope": {"enabled": True}}}))[0] == 400
        assert (await post({"entities": {}}, "text/plain"))[0] == 400  # no cross-site form posts
        assert (await a.http("GET", "/secret", {}, b"", ing))[0] == 404
    asyncio.run(go())


def test_build_config_from_options():
    cfg = addon.build_config({"server": "https://percia.ai/", "max_height": 480, "max_bitrate_kbps": 900, "telemetry": False})
    assert cfg.server == "https://percia.ai" and cfg.video.max_height == 480 and cfg.video.max_bitrate_kbps == 900
    assert cfg.variant == "ha_addon" and cfg.health_listen == ""
    with pytest.raises(Exception):
        addon.build_config({"server": "http://evil.example"})
