import asyncio
import json
import os
import stat

import pytest
from fakes import KEY, TOKEN

from percia_bridge import api as papi
from percia_bridge.logs import redact
from percia_bridge.state import CameraState, State, StateError


def test_pairing_token_from_url_or_bare():
    assert papi.pairing_token("https://percia.ai/cam/abc123XYZ") == "abc123XYZ"
    assert papi.pairing_token("https://percia.ai/cam/abc123XYZ/") == "abc123XYZ"
    assert papi.pairing_token("abc123XYZ") == "abc123XYZ"
    assert papi.pairing_token("https://x/cam/abc?x=1#y") == "abc"


def test_read_api_key_env_wins_and_file_must_be_private(tmp_path, monkeypatch):
    f = tmp_path / "k"
    f.write_text("filekey1234\n")
    os.chmod(f, 0o644)
    monkeypatch.delenv("PERCIA_API_KEY", raising=False)
    with pytest.raises(papi.ApiError, match="chmod 600"):
        papi.read_api_key(file=f)
    os.chmod(f, 0o600)
    assert papi.read_api_key(file=f) == "filekey1234"
    monkeypatch.setenv("PERCIA_API_KEY", "envkey5678")
    assert papi.read_api_key(file=f) == "envkey5678"
    assert "envkey5678" not in redact("using envkey5678")  # registered for redaction
    monkeypatch.delenv("PERCIA_API_KEY")
    with pytest.raises(papi.ApiError, match="No API key"):
        papi.read_api_key(file=None)


def test_create_and_claim_flow(percia):
    async def go():
        api = papi.PerciaApi(percia.url)
        cs = await api.create_and_claim("Front door", KEY)
        await api.heartbeat(cs.token)
        await api.aclose()
        return cs
    cs = asyncio.run(go())
    assert (cs.camera_id, cs.token) == ("cam_1", TOKEN) and cs.whip_url.endswith("/cam/cam_1/whip")
    assert percia.created == ["Front door"] and percia.claimed == 1 and percia.heartbeats == 1


def test_errors_are_typed(percia):
    async def attempt(fn):
        api = papi.PerciaApi(percia.url)
        try:
            await fn(api)
        except papi.ApiError as e:
            return e
        finally:
            await api.aclose()
    assert isinstance(asyncio.run(attempt(lambda a: a.create_and_claim("x", "wrong"))), papi.Unauthorized)
    assert isinstance(asyncio.run(attempt(lambda a: a.heartbeat("pc_wrong"))), papi.Unauthorized)
    percia.camera_limit = True
    e = asyncio.run(attempt(lambda a: a.create_and_claim("x", KEY)))
    assert e.status == 429 and e.code == "camera_limit"
    percia.camera_limit, percia.cameras_enabled = False, False
    e = asyncio.run(attempt(lambda a: a.create_and_claim("x", KEY)))
    assert isinstance(e, papi.FeatureOff) and "not have cameras enabled" in e.message


def test_delete_camera(percia):
    async def go():
        api = papi.PerciaApi(percia.url)
        await api.delete_camera("cam_1", KEY)
        percia.delete_status = 409
        try:
            await api.delete_camera("cam_1", KEY)
        except papi.ApiError as e:
            return e
        finally:
            await api.aclose()
    e = asyncio.run(go())
    assert e.code == "camera_in_use" and percia.deleted == ["cam_1", "cam_1"]


def test_insecure_whip_url_is_refused():
    with pytest.raises(papi.ApiError, match="non-https"):
        papi._check_https("http://media.example.com/cam/1/whip")
    papi._check_https("https://media.example.com/cam/1/whip")
    papi._check_https("http://127.0.0.1:8889/cam/1/whip")


def test_state_roundtrip_modes_and_corruption(tmp_path):
    d = tmp_path / "state"
    s = State(d)
    s.put("front", CameraState("cam_1", TOKEN, "https://m/w"))
    f = d / "cameras.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(d.stat().st_mode) == 0o700
    s2 = State(d)
    assert s2.get("front").token == TOKEN and s2.install_id == s.install_id and s2.install_id
    assert TOKEN not in redact(f"token {TOKEN}")  # loading the state registers tokens for redaction
    s2.drop("front")
    assert State(d).get("front") is None
    f.write_text("{not json")
    with pytest.raises(StateError, match="not valid JSON"):
        State(d)
    f.write_text(json.dumps({"state_version": 99, "cameras": {}}))
    with pytest.raises(StateError, match="newer percia-bridge"):
        State(d)
