import stat
from pathlib import Path

import pytest

from percia_bridge.config import ConfigError, expand_env, load, parse, write_private

P = Path("/tmp/x/config.yaml")


def test_minimal_defaults():
    c = parse({}, P)
    assert c.server == "https://percia.ai" and c.auto_add and c.cameras == []
    assert c.health_listen == "127.0.0.1:9477" and c.video.mode == "auto" and not c.telemetry


def test_full_camera_list():
    c = parse({"cameras": [
        {"name": "front-door", "source": {"type": "rtsp", "url": "rtsp://u:p@10.0.0.2/s"}},
        {"name": "garage", "source": {"type": "onvif", "host": "10.0.0.3", "username": "a", "password": "b", "profile": "Profile_1"},
         "video": {"mode": "transcode", "max_height": 480}},
        {"name": "drive", "source": {"type": "frigate", "url": "http://frigate:5000", "camera": "drive"}},
    ]}, P)
    assert [x.name for x in c.cameras] == ["front-door", "garage", "drive"]
    assert c.cameras[1].video.mode == "transcode" and c.cameras[1].video.max_height == 480
    assert c.cameras[0].video.mode == "auto"  # inherits default


def test_env_expansion_and_missing():
    assert expand_env({"a": ["x${FOO}y"]}, {"FOO": "1"}) == {"a": ["x1y"]}
    with pytest.raises(ConfigError, match="BAR is not set"):
        expand_env("${BAR}", {})
    c = parse({"cameras": [{"name": "a", "source": {"type": "rtsp", "url": "rtsp://u:${PW}@h/s"}}]}, P, {"PW": "pw"})
    assert c.cameras[0].source.url == "rtsp://u:pw@h/s"


@pytest.mark.parametrize("raw,msg", [
    ({"server": "http://percia.ai"}, "https"),
    ({"server": "ftp://x"}, "https"),
    ({"bogus": 1}, "unknown key"),
    ({"video": {"mode": "fast"}}, "mode"),
    ({"video": {"max_fps": 0}}, "max_fps"),
    ({"rtsp_transport": "http"}, "rtsp_transport"),
    ({"cameras": [{"name": "", "source": {"type": "rtsp", "url": "rtsp://h"}}]}, "name"),
    ({"cameras": [{"name": "../x", "source": {"type": "rtsp", "url": "rtsp://h"}}]}, "name"),
    ({"cameras": [{"name": "a", "source": {"type": "rtsp", "url": "http://h"}}]}, "rtsp"),
    ({"cameras": [{"name": "a", "source": {"type": "ftp"}}]}, "type"),
    ({"cameras": [{"name": "a", "source": {"type": "onvif"}}]}, "host"),
    ({"cameras": [{"name": "a", "source": {"type": "frigate", "url": "http://f"}}]}, "frigate"),
    ({"cameras": [{"name": "a", "colour": 1, "source": {"type": "rtsp", "url": "rtsp://h"}}]}, "unknown key"),
    ({"cameras": [{"name": "a", "source": {"type": "rtsp", "url": "rtsp://h"}}, {"name": "A", "source": {"type": "rtsp", "url": "rtsp://h"}}]}, "duplicate"),
    ({"cameras": [{"name": f"c{i}", "source": {"type": "rtsp", "url": "rtsp://h"}} for i in range(11)]}, "at most 10"),
])
def test_rejects_bad_config(raw, msg):
    with pytest.raises(ConfigError, match=msg):
        parse(raw, P)


def test_http_allowed_only_for_localhost():
    assert parse({"server": "http://127.0.0.1:8787/"}, P).server == "http://127.0.0.1:8787"
    with pytest.raises(ConfigError):
        parse({"server": "http://192.168.1.5"}, P)


def test_write_private_modes(tmp_path):
    f = tmp_path / "newdir" / "k"
    write_private(f, "secret")
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700


def test_load_missing_and_invalid(tmp_path):
    with pytest.raises(ConfigError, match="init"):
        load(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load(bad)
