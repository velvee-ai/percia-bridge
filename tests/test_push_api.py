"""The camera-push API client and CLI against a fake Percia that speaks the contract (and an older one that does not)."""
import asyncio
import json
from pathlib import Path

import pytest
import yaml
from fakes import KEY, TOKEN
from test_bridge import cam, make
from test_supervisor import until

from percia_bridge import cli
from percia_bridge.api import ApiError, FeatureOff, Forbidden, PerciaApi, WrongKind, describe_exception
from percia_bridge.state import CameraState


@pytest.fixture
def push(percia):
    percia.push = True
    return percia


def reg(percia, name="front", ext="pb-abc", known=None, key=KEY):
    async def go():
        api = PerciaApi(percia.url)
        try:
            return await api.register(name, key, ext, known), api.push_supported
        finally:
            await api.aclose()
    return asyncio.run(go())


def test_register_creates_push_camera_and_returns_token_once(push):
    r, supported = reg(push)
    assert supported is True and r.flow == "push" and not r.existing and not r.rotated
    assert r.state.token == TOKEN and r.state.kind == "push" and r.state.external_id == "pb-abc"
    assert r.state.whip_url.endswith("/cam/cam_push1/whip") and push.created == ["front"] and push.claimed == 0


def test_register_is_idempotent_and_reuses_the_stored_token(push):
    first, _ = reg(push)
    again, _ = reg(push, known=first.state)
    assert again.existing and not again.rotated and again.state.token == first.state.token
    assert push.created == ["front"] and len(push.push_cams) == 1 and push.rotated == []


def test_register_after_lost_state_rotates_instead_of_duplicating(push):
    reg(push)
    again, _ = reg(push, known=None)  # state dir wiped: Percia has the camera but will not show its token again
    assert again.existing and again.rotated and again.state.token != TOKEN
    assert len(push.push_cams) == 1 and push.rotated == ["cam_push1"]


def test_other_external_id_is_another_camera(push):
    reg(push, "a", "pb-1")
    reg(push, "b", "pb-2")
    assert len(push.push_cams) == 2


def test_older_server_that_ignores_kind_falls_back_to_pairing_claim(percia):
    r, supported = reg(percia)  # legacy fake answers with pairUrl, no whipUrl
    assert supported is False and r.flow == "claim" and r.state.kind == "phone" and r.state.token == TOKEN
    assert percia.created == ["front"] and percia.claimed == 1


def test_older_strict_server_that_rejects_new_fields_falls_back(percia):
    percia.strict_create = True
    r, supported = reg(percia)
    assert supported is False and r.flow == "claim" and percia.created == ["front"]


def test_fallback_is_learned_once(percia):
    percia.strict_create = True

    async def go():
        api = PerciaApi(percia.url)
        await api.register("a", KEY, "x")
        await api.register("b", KEY, "y")
        await api.aclose()
    asyncio.run(go())
    assert percia.created == ["a", "b"]


def test_cameras_switched_off_is_a_clear_error(push):
    push.cameras_enabled = False
    with pytest.raises(FeatureOff, match="not have cameras enabled"):
        reg(push)


def test_camera_limit_and_scope_errors_are_human(push):
    push.camera_limit = True
    with pytest.raises(ApiError, match="already has 10 cameras"):
        reg(push)
    push.camera_limit = False
    push.scopes = ["cameras:read", "cameras:write"]
    first, _ = reg(push)
    async def rotate():
        api = PerciaApi(push.url)
        try:
            await api.rotate_token(first.state.camera_id, KEY)
        finally:
            await api.aclose()
    with pytest.raises(Forbidden, match="scope"):
        asyncio.run(rotate())


def test_rotate_on_phone_camera_is_wrong_kind(percia):
    async def go():
        api = PerciaApi(percia.url)
        try:
            await api.rotate_token("cam_1", KEY)
        finally:
            await api.aclose()
    with pytest.raises(WrongKind):
        asyncio.run(go())


def test_describe_exception_is_never_a_traceback():
    import httpx
    assert "DNS" in describe_exception(httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known"))
    assert "timeout" in describe_exception(httpx.ReadTimeout("x"))
    assert "TLS" in describe_exception(httpx.ConnectError("certificate verify failed"))
    assert "outbound" in describe_exception(httpx.ConnectError("Connection refused"))


# ---------------- CLI ----------------
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PERCIA_BRIDGE_CONFIG", str(tmp_path / "cfg" / "config.yaml"))
    monkeypatch.delenv("PERCIA_API_KEY", raising=False)
    monkeypatch.delenv("PERCIA_BRIDGE_STATE", raising=False)
    return tmp_path


def run(*argv):
    return cli.main(list(argv))


def init(percia, monkeypatch):
    monkeypatch.setenv("TESTKEY", KEY)
    assert run("init", "--server", percia.url, "--api-key-env", "TESTKEY") == 0


def test_add_then_wiped_state_then_add_again_never_duplicates(home, push, monkeypatch, capsys):
    init(push, monkeypatch)
    assert run("add", "front", "--rtsp", "rtsp://u:pw@10.0.0.2/s") == 0
    state = home / "cfg" / "state" / "cameras.json"
    assert json.loads(state.read_text())["cameras"]["front"]["kind"] == "push"
    state.unlink()  # reinstall: the state dir is gone but the config stayed
    assert run("register") == 0
    out = capsys.readouterr().out
    assert "already had this camera" in out and "new token issued" in out
    assert len(push.push_cams) == 1 and push.rotated == ["cam_push1"]
    ext = yaml.safe_load((home / "cfg" / "config.yaml").read_text())["cameras"][0]["external_id"]
    assert list(push.push_cams.values())[0]["externalId"] == ext


def test_same_camera_with_other_login_is_the_same_externalId(home, push, monkeypatch):
    from percia_bridge.config import parse
    from percia_bridge.identity import external_id
    a = parse({"cameras": [{"name": "a", "source": {"type": "rtsp", "url": "rtsp://u:p@10.0.0.2:554/s1"}}]}, Path("c")).cameras[0]
    b = parse({"cameras": [{"name": "renamed", "source": {"type": "rtsp", "url": "rtsp://admin:other@10.0.0.2/s1"}}]}, Path("c")).cameras[0]
    c = parse({"cameras": [{"name": "c", "source": {"type": "rtsp", "url": "rtsp://u:p@10.0.0.3/s1"}}]}, Path("c")).cameras[0]
    assert external_id(a) == external_id(b) != external_id(c)
    assert "10.0.0.2" not in external_id(a) and external_id(a).startswith("pb-")
    f1 = parse({"cameras": [{"name": "x", "source": {"type": "frigate", "url": "http://f:5000", "camera": "porch"}}]}, Path("c")).cameras[0]
    ha = parse({"cameras": [{"name": "y", "source": {"type": "homeassistant", "camera": "camera.porch"}}]}, Path("c")).cameras[0]
    assert external_id(f1) != external_id(ha)
    forced = parse({"cameras": [{"name": "z", "external_id": "my-id.1", "source": {"type": "rtsp", "url": "rtsp://h/s"}}]}, Path("c")).cameras[0]
    assert external_id(forced) == "my-id.1"


def test_rotate_stores_new_token_and_old_one_stops(home, push, monkeypatch, capsys):
    init(push, monkeypatch)
    run("add", "front", "--rtsp", "rtsp://h/s")
    assert run("rotate", "front") == 0
    st = json.loads((home / "cfg" / "state" / "cameras.json").read_text())["cameras"]["front"]
    assert st["token"] != TOKEN and push.rotated == ["cam_push1"] and st["kind"] == "push"
    assert st["token"] not in capsys.readouterr().out


def test_rotate_on_old_flow_camera_explains_what_to_do(home, percia, monkeypatch, capsys):
    init(percia, monkeypatch)
    run("add", "front", "--rtsp", "rtsp://h/s")
    with pytest.raises(SystemExit):
        run("rotate", "front")
    assert "add front --force" in capsys.readouterr().err


def test_rotate_unknown_camera_and_revoke(home, push, monkeypatch, capsys):
    init(push, monkeypatch)
    with pytest.raises(SystemExit):
        run("rotate", "nope")
    run("add", "front", "--rtsp", "rtsp://h/s")
    assert run("rotate", "front", "--revoke") == 0 and push.revoked_tokens == ["cam_push1"]
    assert "publishing is refused" in capsys.readouterr().out


def test_register_all_configured_cameras_in_bulk(home, push, monkeypatch, capsys):
    init(push, monkeypatch)
    cfgp = home / "cfg" / "config.yaml"
    raw = yaml.safe_load(cfgp.read_text())
    raw["cameras"] = [{"name": f"cam{i}", "source": {"type": "rtsp", "url": f"rtsp://10.0.0.{i}/s"}} for i in range(1, 6)]
    cfgp.write_text(yaml.safe_dump(raw))
    assert run("register") == 0
    assert len(push.push_cams) == 5 and push.created == [f"cam{i}" for i in range(1, 6)]
    assert run("register") == 0 and len(push.push_cams) == 5  # nothing more to do
    assert "already registered" in capsys.readouterr().out


def test_bulk_stops_early_on_bad_key_and_reports_each(home, push, monkeypatch, capsys):
    init(push, monkeypatch)
    cfgp = home / "cfg" / "config.yaml"
    raw = yaml.safe_load(cfgp.read_text())
    raw["cameras"] = [{"name": f"c{i}", "source": {"type": "rtsp", "url": f"rtsp://10.0.0.{i}/s"}} for i in range(1, 4)]
    cfgp.write_text(yaml.safe_dump(raw))
    (home / "cfg" / "api_key").write_text("bad-key\n")
    assert run("register") == 1
    err = capsys.readouterr().err
    assert err.count("FAIL") == 3 and "rejected the API key" in err and push.created == []


def write_cams(home, n, start=1):
    cfgp = home / "cfg" / "config.yaml"
    raw = yaml.safe_load(cfgp.read_text())
    raw["cameras"] = [{"name": f"cam{i}", "source": {"type": "rtsp", "url": f"rtsp://10.0.0.{i}/s"}} for i in range(start, start + n)]
    cfgp.write_text(yaml.safe_dump(raw))


def test_register_uses_one_bulk_call_and_is_idempotent(home, push, monkeypatch, capsys):
    init(push, monkeypatch)
    write_cams(home, 5)
    assert run("register") == 0
    assert len(push.bulk_calls) == 1 and len(push.bulk_calls[0]) == 5 and len(push.push_cams) == 5
    statef = home / "cfg" / "state" / "cameras.json"
    assert all(v["token"].startswith("pc_") and v["kind"] == "push" for v in json.loads(statef.read_text())["cameras"].values())
    # a wiped state directory: every camera exists on Percia (token null), so tokens are rotated, nothing is duplicated
    statef.unlink()
    capsys.readouterr()
    assert run("register") == 0
    out = capsys.readouterr().out
    assert len(push.push_cams) == 5 and len(push.rotated) == 5 and out.count("already had this camera") == 5
    assert len(push.bulk_calls) == 2 and push.bulk_calls[0] == push.bulk_calls[1]  # same externalIds both times


def test_bulk_single_camera_uses_the_single_route(home, push, monkeypatch):
    init(push, monkeypatch)
    write_cams(home, 1)
    assert run("register") == 0 and push.bulk_calls == [] and len(push.push_cams) == 1


def test_bulk_chunks_of_ten(push):
    from percia_bridge.api import BULK_MAX
    assert BULK_MAX == 10

    async def go():
        api = PerciaApi(push.url)
        try:
            return await api.register_bulk([(f"c{i}", f"pb-{i}", None) for i in range(10)], KEY)
        finally:
            await api.aclose()
    out = asyncio.run(go())
    assert [type(o).__name__ for o in out] == ["Registration"] * 10 and [len(c) for c in push.bulk_calls] == [10]

    push.push_cams.clear()
    push.bulk_calls.clear()

    async def go2():  # 12 cameras = a chunk of 10 and a chunk of 2; the server's limit of 10 makes the second chunk fail alone
        api = PerciaApi(push.url)
        try:
            return await api.register_bulk([(f"c{i}", f"pb-{i}", None) for i in range(12)], KEY)
        finally:
            await api.aclose()
    out = asyncio.run(go2())
    assert [len(c) for c in push.bulk_calls] == [10, 2]
    assert all(not isinstance(o, Exception) for o in out[:10]) and all(isinstance(o, ApiError) and o.code == "camera_limit" for o in out[10:])


def test_bulk_camera_limit_is_all_or_nothing_with_a_clear_message(home, push, monkeypatch, capsys):
    init(push, monkeypatch)
    write_cams(home, 3)
    push.camera_limit = True
    assert run("register") == 1
    err = capsys.readouterr().err
    assert err.count("FAIL") == 3 and "10 cameras" in err and push.push_cams == {}
    push.camera_limit = False
    assert run("register") == 0 and len(push.push_cams) == 3  # nothing was half-created, so a retry just works


def test_bulk_plan_limit_message_is_per_camera(home, push, monkeypatch, capsys):
    init(push, monkeypatch)
    write_cams(home, 2)
    push.plan_limit = True
    assert run("register") == 1
    err = capsys.readouterr().err
    assert err.count("FAIL") == 2 and "plan_limit" in err and push.push_cams == {}


def test_bulk_falls_back_to_the_loop_on_a_server_without_the_route(home, push, monkeypatch, capsys):
    init(push, monkeypatch)
    push.bulk = False  # push API yes, bulk route 404
    write_cams(home, 3)
    assert run("register") == 0
    assert push.bulk_calls == [] and len(push.push_cams) == 3 and push.created == ["cam1", "cam2", "cam3"]


def test_bulk_not_used_against_an_older_pairing_server(home, percia, monkeypatch):
    init(percia, monkeypatch)  # percia.push is False: kind is ignored, pairing flow
    write_cams(home, 2)
    assert run("register") == 0 and percia.bulk_calls == [] and percia.created == ["cam1", "cam2"] and percia.claimed == 2


def test_bulk_400_falls_back_per_camera_so_the_bad_one_is_named(push):
    async def go():
        api = PerciaApi(push.url)
        try:
            return await api.register_bulk([("good", "pb-1", None), ("bad", "pb-1", None)], KEY)  # duplicate externalId: the server says 400
        finally:
            await api.aclose()
    out = asyncio.run(go())
    assert not isinstance(out[0], Exception) and not isinstance(out[1], Exception)  # the single route is idempotent: second is the same camera
    assert len(push.push_cams) == 1


def test_bulk_duplicate_external_ids_in_the_config_register_once(home, push, monkeypatch):
    init(push, monkeypatch)
    cfgp = home / "cfg" / "config.yaml"
    raw = yaml.safe_load(cfgp.read_text())
    raw["cameras"] = [{"name": n, "source": {"type": "rtsp", "url": "rtsp://10.0.0.9/s"}} for n in ("a", "b")] + [
        {"name": "c", "source": {"type": "rtsp", "url": "rtsp://10.0.0.10/s"}}, {"name": "d", "source": {"type": "rtsp", "url": "rtsp://10.0.0.11/s"}}]
    cfgp.write_text(yaml.safe_dump(raw))
    run("register")
    assert len(push.push_cams) == 3 and all(len(set(c)) == len(c) for c in push.bulk_calls)


def test_add_all_from_frigate_config(home, push, monkeypatch, capsys):
    init(push, monkeypatch)
    fx = Path(__file__).parent / "fixtures" / "frigate_config.yml"
    assert run("add-all", "--frigate-config", str(fx), "--frigate", "http://frigate:5000") == 0
    cams = yaml.safe_load((home / "cfg" / "config.yaml").read_text())["cameras"]
    assert [c["name"] for c in cams] == ["porch"] and cams[0]["source"]["type"] == "frigate" and cams[0]["external_id"].startswith("pb-")
    assert run("add-all", "--frigate-config", str(fx), "--frigate", "http://frigate:5000") == 0  # again: skipped, no duplicate
    assert len(push.push_cams) == 1 and "already in the config" in capsys.readouterr().out


def test_status_uses_the_status_route_for_state_and_hint(home, push, monkeypatch, capsys):
    push.cameras_status_route = True
    init(push, monkeypatch)
    run("add", "front", "--rtsp", "rtsp://h/s")
    capsys.readouterr()
    assert run("status", "--listen", "127.0.0.1:1") == 1  # no local bridge running
    out = capsys.readouterr().out
    assert push.status_calls == 1 and "waiting" in out and "hint for waiting" in out and "never" in out  # registered, never published
    push.push_cams["cam_push1"]["lastSeenAt"] = "2020-01-01T00:00:00Z"
    run("status", "--listen", "127.0.0.1:1")
    out = capsys.readouterr().out
    assert "yes" in out and "ago" in out and "hint for" not in out and "h264" not in out  # the final contract has no codec/resolution yet
    push.push_cams["cam_push1"]["stale"] = True
    run("status", "--listen", "127.0.0.1:1")
    out = capsys.readouterr().out
    assert "offline" in out and "hint for offline" in out
    push.push_cams["cam_push1"]["token"] = None
    run("status", "--listen", "127.0.0.1:1")
    assert "revoked" in capsys.readouterr().out


def test_status_shows_codec_resolution_fps_bitrate_when_the_server_reports_them(home, push, monkeypatch, capsys):
    push.cameras_status_route = True
    push.status_video = True
    init(push, monkeypatch)
    run("add", "front", "--rtsp", "rtsp://h/s")
    push.push_cams["cam_push1"]["lastSeenAt"] = "2020-01-01T00:00:00Z"
    capsys.readouterr()
    run("status", "--listen", "127.0.0.1:1")
    assert "h264 640x360 15fps 1.5Mbit/s" in capsys.readouterr().out


def test_status_without_status_route_falls_back_to_list_and_asks_once(home, push, monkeypatch, capsys):
    init(push, monkeypatch)  # cameras_status_route False: an older server answers 404
    run("add", "front", "--rtsp", "rtsp://h/s")
    run("add", "back", "--rtsp", "rtsp://h/t")
    capsys.readouterr()
    run("status", "--listen", "127.0.0.1:1")
    out = capsys.readouterr().out
    assert "front" in out and "back" in out and "never" in out and "no" in out and "hint" not in out


def test_video_summary_handles_missing_and_partial_fields():
    assert cli.video_summary(None) == "-" and cli.video_summary({"state": "online"}) == "-"
    assert cli.video_summary({"video": {"codec": "h265", "fps": 9.6}}) == "h265 10fps"


def test_init_explains_missing_scope_and_cameras_off(home, push, monkeypatch, capsys):
    monkeypatch.setenv("TESTKEY", KEY)
    push.scopes = ["watches:read"]
    with pytest.raises(SystemExit):
        run("init", "--server", push.url, "--api-key-env", "TESTKEY")
    assert "scope" in capsys.readouterr().err
    assert not (home / "cfg" / "api_key").exists()


def test_init_warns_only_when_watches_delete_is_missing(home, push, monkeypatch, capsys):
    monkeypatch.setenv("TESTKEY", KEY)
    push.scopes = ["cameras:read", "cameras:write"]
    assert run("init", "--server", push.url, "--api-key-env", "TESTKEY") == 0
    out = capsys.readouterr().out
    assert "watches:delete" in out and "Next steps" in out and "percia-bridge doctor" in out


def test_init_cameras_off_is_fatal(home, push, monkeypatch, capsys):
    monkeypatch.setenv("TESTKEY", KEY)
    push.cameras_enabled = False
    with pytest.raises(SystemExit):
        run("init", "--server", push.url, "--api-key-env", "TESTKEY")
    assert "not have cameras enabled" in capsys.readouterr().err
    assert not (home / "cfg" / "api_key").exists()


def test_check_command_and_helpful_config_errors(home, push, monkeypatch, capsys):
    init(push, monkeypatch)
    assert run("check") == 0
    cfgp = home / "cfg" / "config.yaml"
    raw = yaml.safe_load(cfgp.read_text())
    raw["autoadd"] = True
    cfgp.write_text(yaml.safe_dump(raw))
    with pytest.raises(SystemExit):
        run("check")
    assert "did you mean auto_add" in capsys.readouterr().err
    raw = yaml.safe_load(cfgp.read_text())
    del raw["autoadd"]
    raw["cameras"] = [{"name": "a", "source": {"type": "rtp", "url": "x"}}]
    cfgp.write_text(yaml.safe_dump(raw))
    with pytest.raises(SystemExit):
        run("check")
    assert "must be one of" in capsys.readouterr().err


# ---------------- bridge: auto-add, rotate while running ----------------
def test_bridge_auto_add_uses_push_and_stays_idempotent(tmp_path, push):
    async def go():
        b, runs, api = make(tmp_path, push)
        await b.apply([cam("a")])
        assert await until(lambda: b.health()["cameras"][0]["state"] == "streaming")
        await b.shutdown()
        st = json.loads((tmp_path / "st" / "cameras.json").read_text())["cameras"]["a"]
        assert st["kind"] == "push" and st["external_id"].startswith("pb-")
        (tmp_path / "st" / "cameras.json").unlink()  # wiped state: re-registers idempotently
        b2, _, api2 = make(tmp_path, push)
        await b2.apply([cam("a")])
        assert await until(lambda: b2.health()["cameras"][0]["state"] == "streaming")
        await b2.shutdown()
        await api.aclose()
        await api2.aclose()
    asyncio.run(go())
    assert len(push.push_cams) == 1 and push.created == ["a"]


def test_revoked_camera_resumes_after_rotate_writes_a_new_token(tmp_path, push):
    from percia_bridge import supervisor as sv
    from percia_bridge.state import State

    async def go():
        b, runs, api = make(tmp_path, push, scenarios=("stream",))
        b.timing = sv.Timing(heartbeat_interval=0.1, stable_after=0.5, stall_after=0.6, startup_timeout=1.5, backoff_base=0.05, backoff_cap=0.2,
                             jitter=0.0, stop_grace=2, revoked_poll=0.1)
        await b.apply([cam("a")])
        assert await until(lambda: b.cameras()[0]["state"] == "streaming")
        push.revoked = True  # Percia now answers 401 to heartbeats
        assert await until(lambda: b.cameras()[0]["state"] == "revoked")
        assert "percia-bridge rotate" in b.cameras()[0]["last_error"]
        push.revoked = False
        other = State(tmp_path / "st")  # what `percia-bridge rotate` does from another process
        old = other.get("a")
        other.put("a", CameraState(old.camera_id, "pc_ROTATEDTOKEN0123456789abcdef", old.whip_url, old.external_id, "push"))
        push.push_cams[old.camera_id]["token"] = "pc_ROTATEDTOKEN0123456789abcdef"
        assert await until(lambda: b.cameras()[0]["state"] == "streaming")
        await b.shutdown()
        await api.aclose()
    asyncio.run(go())


# ---------------- discover prints ready `add` commands ----------------
def test_discover_onvif_prints_ready_add_commands(monkeypatch, capsys):
    from percia_bridge import onvif
    dev = onvif.Device(xaddrs=["http://192.168.1.30:8080/onvif/device_service"], scopes=["onvif://www.onvif.org/name/Front%20Door"], uuid="u")

    async def fake_discover(timeout, bind):
        return [dev]
    monkeypatch.setattr(onvif, "discover", fake_discover)
    assert run("discover", "--no-probe") == 0
    out = capsys.readouterr().out
    assert "Found 1 ONVIF device" in out
    assert "percia-bridge add Front-Door --onvif 192.168.1.30 --port 8080 --user <user> --password-env ONVIF_PASSWORD" in out


def test_discover_onvif_none_found_explains_why(monkeypatch, capsys):
    from percia_bridge import onvif

    async def none(timeout, bind):
        return []
    monkeypatch.setattr(onvif, "discover", none)
    run("discover")
    out = capsys.readouterr().out
    assert "network_mode: host" in out and "ONVIF off" in out and "--rtsp" in out


def test_discover_frigate_suggests_add_all(capsys, tmp_path):
    cfg = tmp_path / "f.yml"
    cfg.write_text("cameras:\n  porch: {ffmpeg: {inputs: [{path: 'rtsp://127.0.0.1:8554/porch', roles: [detect]}]}}\n  yard: {ffmpeg: {inputs: [{path: 'rtsp://127.0.0.1:8554/yard', roles: [detect]}]}}\n")
    assert run("discover", "--frigate-config", str(cfg), "--frigate", "http://frigate:5000") == 0
    out = capsys.readouterr().out
    assert "percia-bridge add porch --frigate http://frigate:5000 --camera porch" in out and "add-all --frigate http://frigate:5000" in out


def test_discover_home_assistant_prints_add_commands(monkeypatch, capsys):
    from percia_bridge import homeassistant as ha
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")

    async def fake_list(token, *a, **k):
        return ha.parse_states([{"entity_id": "camera.front_door", "state": "idle", "attributes": {"friendly_name": "Front Door"}}])
    monkeypatch.setattr(ha, "list_cameras", fake_list)
    assert run("discover", "--home-assistant") == 0
    assert "percia-bridge add Front Door --homeassistant camera.front_door" in capsys.readouterr().out


def test_discover_home_assistant_outside_the_addon(monkeypatch, capsys):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert run("discover", "--home-assistant") == 1
    assert "add-on" in capsys.readouterr().out
