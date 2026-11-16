import asyncio
import json
import sys
from pathlib import Path

from fakes import KEY
from test_supervisor import FAKE, FAST, until

from percia_bridge import supervisor as sv
from percia_bridge.api import PerciaApi
from percia_bridge.bridge import Bridge, make_resolver
from percia_bridge.config import CameraCfg, Config, Source, Video, parse
from percia_bridge.state import State


def cam(name="a", url="rtsp://h/s"):
    return CameraCfg(name, Source("rtsp", url=url), Video())


def make(tmp_path, percia, key=lambda: KEY, scenarios=("stream",), auto_add=True):
    cfg = Config(path=tmp_path / "c.yaml", state_dir=tmp_path / "st", auto_add=auto_add)
    runs = []

    def builder(ffmpeg, url, whip, token, plan, video, transport, headers):
        runs.append(url)
        return [sys.executable, FAKE, scenarios[min(len(runs) - 1, len(scenarios) - 1)]]

    async def prober(*a):
        return None
    api = PerciaApi(percia.url)
    return Bridge(cfg, api, State(cfg.state_dir), make_resolver(cfg), key, None, FAST, command_builder=builder, prober=prober), runs, api


def test_auto_add_creates_claims_and_streams(tmp_path, percia):
    async def go():
        b, runs, api = make(tmp_path, percia)
        await b.apply([cam("a")])
        assert await until(lambda: b.health()["cameras"][0]["state"] == "streaming")
        assert percia.created == ["a"] and percia.claimed == 1
        assert b.health()["status"] == "ok"
        assert json.loads((tmp_path / "st" / "cameras.json").read_text())["cameras"]["a"]["camera_id"] == "cam_1"
        await b.shutdown()
        await api.aclose()
    asyncio.run(go())


def test_existing_state_is_reused_not_recreated(tmp_path, percia):
    async def go():
        b, runs, api = make(tmp_path, percia)
        await b.apply([cam("a")])
        assert await until(lambda: b.cameras()[0]["state"] == "streaming")
        await b.shutdown()
        b2, _, api2 = make(tmp_path, percia)
        await b2.apply([cam("a")])
        assert await until(lambda: b2.cameras()[0]["state"] == "streaming")
        await b2.shutdown()
        await api.aclose()
        await api2.aclose()
    asyncio.run(go())
    assert percia.created == ["a"]  # only once


def test_auto_add_off_reports_not_added(tmp_path, percia):
    async def go():
        b, runs, api = make(tmp_path, percia, auto_add=False)
        await b.apply([cam("a")])
        assert await until(lambda: b.cameras()[0]["state"] == "setup_failed")
        assert "percia-bridge add a" in b.cameras()[0]["last_error"]
        assert b.health()["status"] == "down"
        await b.shutdown()
        await api.aclose()
    asyncio.run(go())
    assert percia.created == []


def test_bad_api_key_and_camera_limit_are_reported_not_retried(tmp_path, percia):
    async def go():
        b, runs, api = make(tmp_path, percia, key=lambda: "wrong-key")
        await b.apply([cam("a")])
        assert await until(lambda: b.cameras()[0]["state"] == "setup_failed")
        assert "API key" in b.cameras()[0]["last_error"]
        await b.shutdown()
        percia.camera_limit = True
        b, runs, api2 = make(tmp_path, percia)
        await b.apply([cam("b")])
        assert await until(lambda: b.cameras()[0]["state"] == "setup_failed")
        assert "camera_limit" in b.cameras()[0]["last_error"]
        await b.shutdown()
        await api.aclose()
        await api2.aclose()
    asyncio.run(go())


def test_feature_off_message(tmp_path, percia):
    percia.cameras_enabled = False

    async def go():
        b, runs, api = make(tmp_path, percia)
        await b.apply([cam("a")])
        assert await until(lambda: b.cameras()[0]["state"] == "setup_failed")
        assert "not have cameras enabled" in b.cameras()[0]["last_error"]
        await b.shutdown()
        await api.aclose()
    asyncio.run(go())


def test_apply_reconciles_add_remove_change(tmp_path, percia):
    async def go():
        b, runs, api = make(tmp_path, percia)
        await b.apply([cam("a"), cam("b")])
        assert await until(lambda: all(c["state"] == "streaming" for c in b.cameras()) and len(b.cameras()) == 2)
        old_a = b.entries["a"].worker
        await b.apply([cam("a"), cam("c")])  # b removed, c added, a untouched
        assert set(b.entries) == {"a", "c"} and b.entries["a"].worker is old_a
        await b.apply([cam("a", url="rtsp://other/s"), cam("c")])  # a changed -> restarted
        assert await until(lambda: b.entries["a"].worker is not None and b.entries["a"].worker is not old_a)
        await b.shutdown()
        await api.aclose()
    asyncio.run(go())


def test_health_degraded_and_metrics(tmp_path, percia):
    async def go():
        b, runs, api = make(tmp_path, percia, scenarios=("stream",))
        await b.apply([cam("good"), cam('we"ird')])
        assert await until(lambda: all(c["state"] == "streaming" for c in b.cameras()) and len(b.cameras()) == 2)
        b.entries["good"].worker.rt.state = sv.BACKOFF
        assert b.health()["status"] == "degraded"
        m = b.metrics()
        assert 'percia_bridge_camera_up{camera="good"} 0' in m and 'camera="we\\"ird"' in m and "percia_bridge_up 1" in m
        status, ctype, body = await b.http("GET", "/healthz", {}, b"", ())
        assert status == 200 and json.loads(body)["status"] == "degraded"
        assert (await b.http("GET", "/nope", {}, b"", ()))[0] == 404
        assert (await b.http("POST", "/healthz", {}, b"", ()))[0] == 405
        for e in b.entries.values():
            e.worker.rt.state = sv.BACKOFF
        assert (await b.http("GET", "/healthz", {}, b"", ()))[0] == 503
        await b.shutdown()
        await api.aclose()
    asyncio.run(go())


def test_resolver_for_each_source_type():
    cfg = parse({"cameras": [
        {"name": "r", "source": {"type": "rtsp", "url": "rtsp://h/s"}},
        {"name": "f", "source": {"type": "frigate", "url": "http://127.0.0.1:1", "camera": "porch"}}]}, Path("c.yaml"))
    res = make_resolver(cfg)
    assert asyncio.run(res(cfg.cameras[0])).url == "rtsp://h/s"
    # Frigate API unreachable -> conventional restream name
    assert asyncio.run(res(cfg.cameras[1])).url == "rtsp://127.0.0.1:8554/porch"
