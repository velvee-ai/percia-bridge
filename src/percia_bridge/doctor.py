"""`percia-bridge doctor`: everything that commonly goes wrong on first setup, checked in one go, each with a fix."""

from __future__ import annotations

import asyncio
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from . import __version__
from .api import ApiError, PerciaApi, describe_exception, read_api_key
from .config import CameraCfg, Config
from .logs import strip_credentials
from .media import Probe, run_probe

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
SYMBOL = {OK: "ok  ", WARN: "WARN", FAIL: "FAIL", INFO: "info"}
MEDIA_HOSTS = ("media.percia.ai", "playground.percia.ai")
REQUIRED_FFMPEG = (7, 1)
OUTBOUND_STATEMENT = (
    "The bridge only makes outbound connections: HTTPS (TCP 443) to Percia, and WebRTC media (UDP, or TCP as fallback) "
    "that your router opens as a reply to the bridge's own outbound request. No port forwarding, no inbound firewall "
    "rule and no VPN are needed. If your firewall blocks outbound UDP, allow outbound UDP to media.percia.ai."
)


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


def parse_ffmpeg_version(line: str) -> tuple[int, int] | None:
    """'ffmpeg version 8.1.2 Copyright...' / 'ffmpeg version n7.1-3-g...' -> (8, 1). Git builds ('N-12345-g...') -> None."""
    m = re.search(r"ffmpeg version n?(\d+)\.(\d+)", line)
    return (int(m.group(1)), int(m.group(2))) if m else None


def check_ffmpeg(ffmpeg: str = "ffmpeg") -> list[Check]:
    exe = shutil.which(ffmpeg)
    if not exe:
        return [Check("ffmpeg", FAIL, f"{ffmpeg} not found", "Install ffmpeg 7.1 or newer with the WHIP muxer (macOS: brew install ffmpeg), or use the Docker image, which has one.")]
    try:
        version = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
        muxers = subprocess.run([exe, "-hide_banner", "-muxers"], capture_output=True, text=True, timeout=10).stdout
        encoders = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError, IndexError) as e:
        return [Check("ffmpeg", FAIL, f"cannot run {exe}: {e}")]
    out: list[Check] = []
    v = parse_ffmpeg_version(version)
    if v and v < REQUIRED_FFMPEG:
        out.append(Check("ffmpeg version", FAIL, version, "ffmpeg 7.1 is the first release with the WHIP muxer; upgrade it."))
    else:
        out.append(Check("ffmpeg version", OK, version))
    if " whip " in muxers:
        out.append(Check("ffmpeg WHIP muxer", OK, "present"))
    else:
        out.append(Check("ffmpeg WHIP muxer", FAIL, "this ffmpeg build has no whip muxer (Debian/Ubuntu packages do not)",
                         "Use the Docker image, a static build from BtbN/FFmpeg-Builds, or Homebrew's ffmpeg."))
    if " libx264 " in encoders:
        out.append(Check("ffmpeg libx264", OK, "present (needed for cameras that must be re-encoded, e.g. H.265)"))
    else:
        out.append(Check("ffmpeg libx264", WARN, "no libx264 encoder: only cameras that already send H.264 without B-frames can be copied",
                         "Use a GPL ffmpeg build with libx264."))
    return out


async def _resolve(host: str) -> str:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return infos[0][4][0]


async def check_host(host: str, client: httpx.AsyncClient) -> tuple[list[Check], httpx.Response | None]:
    try:
        ip = await asyncio.wait_for(_resolve(host), 8)
    except Exception:  # noqa: BLE001
        return [Check(f"DNS {host}", FAIL, "cannot resolve the name", "Check the network and DNS settings (try `nslookup %s`)." % host)], None
    out = [Check(f"DNS {host}", OK, ip)]
    try:
        r = await client.get(f"https://{host}/")
    except Exception as e:  # noqa: BLE001
        out.append(Check(f"HTTPS {host}", FAIL, describe_exception(e), "Outbound TCP 443 must be open (and no TLS-inspecting proxy)."))
        return out, None
    out.append(Check(f"HTTPS {host}", OK, f"reachable (HTTP {r.status_code})"))
    return out, r


def clock_skew_seconds(date_header: str, now: datetime | None = None) -> float | None:
    try:
        server = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    now = now or datetime.now(timezone.utc)
    return (now - server).total_seconds()


def check_clock(resp: httpx.Response | None, host: str) -> Check:
    if resp is None or "date" not in resp.headers:
        return Check("clock", INFO, "could not compare with the server clock")
    skew = clock_skew_seconds(resp.headers["date"])
    if skew is None:
        return Check("clock", INFO, "unreadable Date header from the server")
    if abs(skew) > 300:
        return Check("clock", FAIL, f"this machine's clock is {abs(skew):.0f}s {'ahead of' if skew > 0 else 'behind'} {host}",
                     "Turn on NTP time sync; large skew breaks TLS and ONVIF logins.")
    if abs(skew) > 30:
        return Check("clock", WARN, f"clock skew {skew:+.0f}s versus {host}", "Enable NTP time sync.")
    return Check("clock", OK, f"within {abs(skew):.0f}s of {host}")


def codec_advice(p: Probe | None) -> Check:
    """Pure: what the probe means for the bridge, including the H.264 baseline hint."""
    if p is None:
        return Check("codec", WARN, "could not read the stream (no video within the timeout)", "Check the URL/credentials and that the camera allows another RTSP session.")
    desc = f"{p.codec or '?'}{' ' + p.profile if p.profile else ''} {p.width}x{p.height}"
    if p.codec != "h264":
        return Check("codec", WARN, f"{desc}: will be re-encoded to H.264 baseline (uses CPU)",
                     "In the camera's settings choose H.264 (not H.265/'Smart codec') for the stream or a sub-stream: then it is copied with almost no CPU.")
    if p.has_b_frames > 0:
        return Check("codec", WARN, f"{desc} with B-frames: will be re-encoded (uses CPU)",
                     "Choose H.264 Baseline (or Main without B-frames) in the camera's video settings: then it is copied with almost no CPU.")
    if p.profile and "baseline" not in p.profile.lower():
        return Check("codec", OK, f"{desc}: sent as is (copy). Hint: if live view stutters, set the camera to H.264 Baseline.")
    return Check("codec", OK, f"{desc}: sent as is (copy), almost no CPU")


async def _tcp(host: str, port: int, timeout: float = 5.0) -> str | None:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        return None
    except Exception as e:  # noqa: BLE001
        return type(e).__name__ if str(e) == "" else str(e)


async def check_camera(cam: CameraCfg, cfg: Config, resolver, prober=run_probe) -> list[Check]:
    label = f"camera {cam.name}"
    try:
        inp = await asyncio.wait_for(resolver(cam), 20)
    except Exception as e:  # noqa: BLE001
        return [Check(label, FAIL, f"cannot get the stream address: {strip_credentials(str(e))}",
                      "Check the camera's address, username and password (or that Frigate / Home Assistant is reachable).")]
    u = urlparse(inp.url)
    out: list[Check] = []
    if u.hostname and u.scheme in ("rtsp", "rtsps"):
        err = await _tcp(u.hostname, u.port or (322 if u.scheme == "rtsps" else 554))
        if err:
            return [Check(label, FAIL, f"cannot connect to {u.hostname}:{u.port or 554} ({err})",
                          "Is the camera on and on the same network as this machine? In Docker, check the container can reach the LAN.")]
        out.append(Check(label, OK, f"reachable at {strip_credentials(inp.url)}"))
    else:
        out.append(Check(label, OK, f"stream address {strip_credentials(inp.url)}"))
    out.append(codec_advice(await prober(cfg.ffprobe_bin, inp.url, cfg.rtsp_transport)))
    out[-1].name = f"{label} codec"
    return out


async def run_doctor(cfg: Config | None, config_error: str = "", offline: bool = False, resolver=None,
                     api_key_reader=read_api_key, extra_hosts: tuple[str, ...] = ()) -> list[Check]:
    checks: list[Check] = [Check("percia-bridge", INFO, __version__)]
    checks += check_ffmpeg(cfg.ffmpeg_bin if cfg else "ffmpeg")
    if cfg is None:
        checks.append(Check("config", FAIL, config_error or "no config", "Run `percia-bridge init`."))
    else:
        checks.append(Check("config", OK, f"{cfg.path} ({len(cfg.cameras)} camera{'s' if len(cfg.cameras) != 1 else ''}, server {cfg.server})"))
    if not offline:
        server_host = urlparse(cfg.server).hostname if cfg else "percia.ai"
        hosts = [h for h in dict.fromkeys([server_host or "percia.ai", *MEDIA_HOSTS, *extra_hosts])
                 if h not in ("localhost", "127.0.0.1", "::1")]
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), headers={"user-agent": f"percia-bridge/{__version__}"}) as client:
            first: httpx.Response | None = None
            for h in hosts:
                cs, resp = await check_host(h, client)
                checks += cs
                first = first or resp
        checks.append(check_clock(first, hosts[0] if hosts else "the server"))
        if cfg:
            checks += await check_key(cfg, api_key_reader)
    if cfg and cfg.cameras and resolver:
        for cam in cfg.cameras:
            checks += await check_camera(cam, cfg, resolver)
    elif cfg and not cfg.cameras:
        checks.append(Check("cameras", WARN, "none configured yet", "Find them with `percia-bridge discover`, then `percia-bridge add NAME --rtsp URL`."))
    checks.append(Check("firewall", INFO, OUTBOUND_STATEMENT))
    return checks


async def check_key(cfg: Config, api_key_reader=read_api_key) -> list[Check]:
    try:
        key = api_key_reader(file=cfg.api_key_file)
    except ApiError as e:
        return [Check("API key", WARN, e.message, "Run `percia-bridge init` (cameras already added keep streaming without it).")]
    api = PerciaApi(cfg.server)
    try:
        return key_checks(await api.whoami(key), await api.server_config())
    except ApiError as e:
        return [Check("API key", FAIL, e.message, "Create a new key in the Percia dashboard with scopes cameras:read, cameras:write, watches:delete.")]
    except Exception as e:  # noqa: BLE001
        return [Check("API key", WARN, f"not checked: {describe_exception(e)}")]
    finally:
        await api.aclose()


def key_checks(who: dict, config: dict) -> list[Check]:
    """Pure: whoami + /api/config -> checks (scopes, phone cameras switched on)."""
    out: list[Check] = []
    scopes = who.get("scopes")
    if scopes is None:  # a signed-in session, not a key: everything is allowed
        out.append(Check("API key scopes", OK, "full access"))
    else:
        missing = [s for s in ("cameras:read", "cameras:write") if s not in scopes]
        if missing:
            out.append(Check("API key scopes", FAIL, f"missing {', '.join(missing)}", "Create a key with scopes cameras:read, cameras:write and watches:delete."))
        elif "watches:delete" not in scopes:
            out.append(Check("API key scopes", WARN, "missing watches:delete (needed to rotate a camera token and to remove cameras)",
                             "Add the scope, or create a new key with cameras:read, cameras:write, watches:delete."))
        else:
            out.append(Check("API key scopes", OK, "cameras:read, cameras:write, watches:delete"))
    if config.get("phoneCameraEnabled") is False:
        out.append(Check("cameras enabled", FAIL, "this Percia server has cameras switched off", "Ask the Percia operator to enable phone/push cameras."))
    elif config:
        out.append(Check("cameras enabled", OK, "on"))
    return out


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"[{SYMBOL[c.status]}] {c.name}: {c.detail}")
        if c.fix and c.status in (WARN, FAIL):
            lines.append(f"         fix: {c.fix}")
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    lines.append("")
    lines.append("All good." if not fails and not warns else f"{fails} problem(s), {warns} warning(s).")
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0


