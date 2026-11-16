import asyncio
import socket
import stat
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx

from percia_bridge import cli
from percia_bridge import doctor as d
from percia_bridge import supervisor as sv
from percia_bridge.config import parse
from percia_bridge.media import Probe


def by_name(checks, name):
    return next(c for c in checks if c.name == name)


def fake_ffmpeg(tmp_path, version="ffmpeg version 8.1.2 Copyright", whip=True, x264=True):
    exe = tmp_path / "ffmpeg"
    exe.write_text(f"""#!/bin/sh
case "$1" in
  -version) echo "{version}";;
  -hide_banner) case "$2" in
     -muxers) echo " E  {'whip ' if whip else 'webm '}  muxer";;
     -encoders) echo " V....D {'libx264' if x264 else 'mpeg4'}  encoder";;
  esac;;
esac
""")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(exe)


def test_ffmpeg_version_parsing():
    assert d.parse_ffmpeg_version("ffmpeg version 8.1.2 Copyright (c)") == (8, 1)
    assert d.parse_ffmpeg_version("ffmpeg version n7.1-3-gabc") == (7, 1)
    assert d.parse_ffmpeg_version("ffmpeg version N-120000-gabc") is None


def test_check_ffmpeg_ok_missing_old_and_no_whip(tmp_path):
    ok = d.check_ffmpeg(fake_ffmpeg(tmp_path))
    assert [c.status for c in ok] == [d.OK, d.OK, d.OK]
    assert d.check_ffmpeg("definitely-not-ffmpeg")[0].status == d.FAIL
    assert by_name(d.check_ffmpeg(fake_ffmpeg(tmp_path, "ffmpeg version 6.0 Copyright")), "ffmpeg version").status == d.FAIL
    nowhip = by_name(d.check_ffmpeg(fake_ffmpeg(tmp_path, whip=False)), "ffmpeg WHIP muxer")
    assert nowhip.status == d.FAIL and "Docker" in nowhip.fix
    assert by_name(d.check_ffmpeg(fake_ffmpeg(tmp_path, x264=False)), "ffmpeg libx264").status == d.WARN


def test_codec_advice_h264_baseline_hint():
    assert d.codec_advice(Probe("h264", "Constrained Baseline", 1280, 720, 0)).status == d.OK
    high = d.codec_advice(Probe("h264", "High", 1920, 1080, 0))
    assert high.status == d.OK and "Baseline" in high.detail
    b = d.codec_advice(Probe("h264", "Main", 1920, 1080, 2))
    assert b.status == d.WARN and "B-frames" in b.detail and "Baseline" in b.fix
    h265 = d.codec_advice(Probe("hevc", "Main", 2560, 1440, 0))
    assert h265.status == d.WARN and "re-encoded" in h265.detail and "H.264" in h265.fix
    assert d.codec_advice(None).status == d.WARN


def test_key_checks_scopes_and_switch():
    full = d.key_checks({"scopes": ["cameras:read", "cameras:write", "watches:delete"]}, {"phoneCameraEnabled": True})
    assert [c.status for c in full] == [d.OK, d.OK]
    assert d.key_checks({"scopes": ["cameras:read", "cameras:write"]}, {})[0].status == d.WARN
    assert d.key_checks({"scopes": ["watches:read"]}, {})[0].status == d.FAIL
    assert d.key_checks({"scopes": None}, {})[0].status == d.OK
    assert by_name(d.key_checks({"scopes": None}, {"phoneCameraEnabled": False}), "cameras enabled").status == d.FAIL


def test_clock_skew():
    now = datetime.now(timezone.utc)
    resp = lambda dt: httpx.Response(200, headers={"date": format_datetime(dt, usegmt=True)})  # noqa: E731
    assert d.check_clock(resp(now), "h").status == d.OK
    assert d.check_clock(resp(now - timedelta(seconds=120)), "h").status == d.WARN
    far = d.check_clock(resp(now - timedelta(hours=2)), "h")
    assert far.status == d.FAIL and "ahead" in far.detail and "NTP" in far.fix
    assert d.check_clock(None, "h").status == d.INFO


def test_check_camera_unreachable_and_reachable(tmp_path):
    cfg = parse({"cameras": [{"name": "a", "source": {"type": "rtsp", "url": "rtsp://u:secret@127.0.0.1:1/s"}}]}, Path("c"))

    async def resolver(cam):
        return sv.ResolvedInput(cam.source.url)

    async def prober(*a):
        return Probe("h264", "Baseline", 640, 360, 0)

    bad = asyncio.run(d.check_camera(cfg.cameras[0], cfg, resolver, prober))
    assert bad[0].status == d.FAIL and "secret" not in bad[0].detail and "127.0.0.1:1" in bad[0].detail

    async def go():
        srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        cfg2 = parse({"cameras": [{"name": "a", "source": {"type": "rtsp", "url": f"rtsp://u:secret@127.0.0.1:{port}/s"}}]}, Path("c"))
        out = await d.check_camera(cfg2.cameras[0], cfg2, resolver, prober)
        srv.close()
        return out
    out = asyncio.run(go())
    assert [c.status for c in out] == [d.OK, d.OK] and "secret" not in out[0].detail and out[1].name == "camera a codec"


def test_check_camera_resolver_failure_has_a_fix():
    cfg = parse({"cameras": [{"name": "a", "source": {"type": "onvif", "host": "10.9.9.9"}}]}, Path("c"))

    async def resolver(cam):
        raise RuntimeError("the camera rejected the ONVIF username/password")
    out = asyncio.run(d.check_camera(cfg.cameras[0], cfg, resolver))
    assert out[0].status == d.FAIL and "password" in out[0].fix


def test_check_host_dns_failure_and_https(monkeypatch):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as c:
            async def resolve_ok(h):
                return "1.2.3.4"
            monkeypatch.setattr(d, "_resolve", resolve_ok)
            ok, resp = await d.check_host("media.percia.ai", c)
            async def resolve_bad(h):
                raise socket.gaierror("nope")
            monkeypatch.setattr(d, "_resolve", resolve_bad)
            bad, none = await d.check_host("media.percia.ai", c)
            return ok, resp, bad, none
    ok, resp, bad, none = asyncio.run(go())
    assert [c.status for c in ok] == [d.OK, d.OK] and "HTTP 404" in ok[1].detail and resp is not None
    assert bad[0].status == d.FAIL and none is None


def test_run_doctor_offline_includes_firewall_statement_and_exit_code(tmp_path):
    cfg = parse({"ffmpeg_bin": fake_ffmpeg(tmp_path), "cameras": []}, tmp_path / "c.yaml")
    checks = asyncio.run(d.run_doctor(cfg, offline=True))
    text = d.render(checks)
    assert "outbound" in text and "No port forwarding" in text and "none configured yet" in text
    assert d.exit_code(checks) == 0
    broken = asyncio.run(d.run_doctor(None, "config file not found", offline=True))
    assert d.exit_code(broken) == 1 and "percia-bridge init" in d.render(broken)


def test_cli_doctor_offline(tmp_path, monkeypatch, capsys):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(f"ffmpeg_bin: {fake_ffmpeg(tmp_path)}\ncameras: []\n")
    cfgp.chmod(0o600)
    monkeypatch.setenv("PERCIA_BRIDGE_CONFIG", str(cfgp))
    assert cli.main(["doctor", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "ffmpeg WHIP muxer" in out and "firewall" in out
    cfgp.write_text("cameras: [")
    assert cli.main(["doctor", "--offline"]) == 1
    assert "invalid YAML" in capsys.readouterr().out
