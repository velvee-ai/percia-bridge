import json
import stat

import pytest
import yaml
from fakes import KEY, TOKEN

from percia_bridge import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PERCIA_BRIDGE_CONFIG", str(tmp_path / "cfg" / "config.yaml"))
    monkeypatch.delenv("PERCIA_API_KEY", raising=False)
    monkeypatch.delenv("PERCIA_BRIDGE_STATE", raising=False)
    return tmp_path


def run(*argv):
    return cli.main(list(argv))


def init(percia, home, monkeypatch, key=KEY):
    monkeypatch.setenv("TESTKEY", key)
    assert run("init", "--server", percia.url, "--api-key-env", "TESTKEY") == 0


def test_init_writes_private_files_and_checks_key(home, percia, monkeypatch, capsys):
    init(percia, home, monkeypatch)
    cfg, key = home / "cfg" / "config.yaml", home / "cfg" / "api_key"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600 and stat.S_IMODE(key.stat().st_mode) == 0o600
    assert key.read_text().strip() == KEY
    assert KEY not in cfg.read_text() and KEY not in capsys.readouterr().out
    assert yaml.safe_load(cfg.read_text())["server"] == percia.url


def test_init_rejects_bad_key_and_existing(home, percia, monkeypatch):
    monkeypatch.setenv("TESTKEY", "wrong")
    with pytest.raises(SystemExit, match="1"):
        run("init", "--server", percia.url, "--api-key-env", "TESTKEY")
    assert not (home / "cfg" / "api_key").exists()
    monkeypatch.setenv("TESTKEY", KEY)
    run("init", "--server", percia.url, "--api-key-env", "TESTKEY")
    with pytest.raises(SystemExit):
        run("init", "--server", percia.url, "--api-key-env", "TESTKEY")
    assert run("init", "--server", percia.url, "--api-key-env", "TESTKEY", "--force") == 0


def test_init_refuses_http_server(home, monkeypatch):
    monkeypatch.setenv("TESTKEY", KEY)
    with pytest.raises(SystemExit, match="2"):
        run("init", "--server", "http://example.com", "--api-key-env", "TESTKEY", "--offline")


def test_add_rtsp_creates_claims_stores_token_and_edits_config(home, percia, monkeypatch, capsys):
    init(percia, home, monkeypatch)
    assert run("add", "front", "--rtsp", "rtsp://u:pw@10.0.0.2/s") == 0
    out = capsys.readouterr().out
    assert "cam_1" in out and TOKEN not in out and "pw" not in out
    assert percia.created == ["front"] and percia.claimed == 1
    st = home / "cfg" / "state" / "cameras.json"
    assert json.loads(st.read_text())["cameras"]["front"]["token"] == TOKEN and stat.S_IMODE(st.stat().st_mode) == 0o600
    cams = yaml.safe_load((home / "cfg" / "config.yaml").read_text())["cameras"]
    assert cams[0]["name"] == "front" and cams[0]["source"] == {"type": "rtsp", "url": "rtsp://u:pw@10.0.0.2/s"}
    assert cams[0]["external_id"].startswith("pb-") and len(cams) == 1
    # idempotent
    assert run("add", "front") == 0
    assert percia.created == ["front"]


def test_add_validation(home, percia, monkeypatch):
    init(percia, home, monkeypatch)
    with pytest.raises(SystemExit):
        run("add", "x")  # no source
    with pytest.raises(SystemExit):
        run("add", "x", "--rtsp", "http://nope")
    with pytest.raises(SystemExit):
        run("add", "../bad", "--rtsp", "rtsp://h/s")
    with pytest.raises(SystemExit):
        run("add", "x", "--frigate", "http://f:5000")  # needs --camera
    assert percia.created == []


def test_add_frigate_and_onvif_password_env_is_kept_as_reference(home, percia, monkeypatch):
    init(percia, home, monkeypatch)
    assert run("add", "porch", "--frigate", "http://frigate:5000", "--camera", "porch") == 0
    cams = yaml.safe_load((home / "cfg" / "config.yaml").read_text())["cameras"]
    assert cams[0]["source"] == {"type": "frigate", "url": "http://frigate:5000", "camera": "porch", "rtsp_port": 8554}


def test_add_force_pairs_again_and_deletes_old(home, percia, monkeypatch):
    init(percia, home, monkeypatch)
    run("add", "front", "--rtsp", "rtsp://h/s")
    assert run("add", "front", "--force") == 0
    assert percia.created == ["front", "front"] and percia.deleted == ["cam_1"]


def test_add_reports_camera_limit_and_feature_off(home, percia, monkeypatch, capsys):
    init(percia, home, monkeypatch)
    percia.camera_limit = True
    assert run("add", "x", "--rtsp", "rtsp://h/s") == 1
    assert "camera_limit" in capsys.readouterr().err
    percia.camera_limit, percia.cameras_enabled = False, False
    assert run("add", "x", "--rtsp", "rtsp://h/s") == 1
    assert "not have cameras enabled" in capsys.readouterr().err


def test_remove(home, percia, monkeypatch, capsys):
    init(percia, home, monkeypatch)
    run("add", "front", "--rtsp", "rtsp://h/s")
    percia.delete_status = 409
    with pytest.raises(SystemExit):
        run("remove", "front")
    assert "watch" in capsys.readouterr().err
    assert yaml.safe_load((home / "cfg" / "config.yaml").read_text())["cameras"]  # untouched on failure
    percia.delete_status = 204
    assert run("remove", "front") == 0
    assert yaml.safe_load((home / "cfg" / "config.yaml").read_text())["cameras"] == []
    assert json.loads((home / "cfg" / "state" / "cameras.json").read_text())["cameras"] == {}
    assert percia.deleted[-1] == "cam_1"


def test_status_without_running_bridge(home, percia, monkeypatch, capsys):
    init(percia, home, monkeypatch)
    run("add", "front", "--rtsp", "rtsp://h/s")
    assert run("status", "--no-remote", "--listen", "127.0.0.1:1") == 1
    out = capsys.readouterr().out
    assert "front" in out and "registered" in out and "No running bridge" in out


def test_discover_frigate_from_config_file(home, capsys):
    from pathlib import Path
    fx = Path(__file__).parent / "fixtures" / "frigate_config.yml"
    assert run("discover", "--frigate-config", str(fx), "--frigate", "http://frigate:5000") == 0
    out = capsys.readouterr().out
    assert "porch  rtsp://frigate:8554/porch" in out and "add with: percia-bridge add porch --frigate http://frigate:5000 --camera porch" in out
    assert "pass" not in out  # the camera password inside go2rtc streams is never printed
