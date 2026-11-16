"""Frigate input: list cameras from /api/config (or a config file) and use Frigate's go2rtc restream, so Frigate keeps the
only connection to the real camera."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

LOCAL_HOSTS = ("127.0.0.1", "localhost", "frigate", "0.0.0.0")


class FrigateError(Exception):
    pass


@dataclass
class FrigateCamera:
    name: str
    stream: str | None  # go2rtc stream name, None when Frigate has no restream for this camera
    enabled: bool = True
    note: str = ""


def _restream_name(path: str, rtsp_port: int) -> str | None:
    u = urlparse(path or "")
    if u.scheme != "rtsp" or (u.port or 8554) != rtsp_port:
        return None
    if u.hostname not in LOCAL_HOSTS:
        return None
    name = u.path.lstrip("/")
    return name or None


def parse_config(cfg: dict, rtsp_port: int = 8554) -> list[FrigateCamera]:
    """Cameras from a Frigate config dict (the /api/config JSON or the YAML file, same shape)."""
    if not isinstance(cfg, dict) or not isinstance(cfg.get("cameras"), dict):
        raise FrigateError("this does not look like a Frigate config (no 'cameras' section)")
    go2rtc_streams = set(((cfg.get("go2rtc") or {}).get("streams") or {}).keys())
    out = []
    for name, cam in cfg["cameras"].items():
        cam = cam or {}
        inputs = ((cam.get("ffmpeg") or {}).get("inputs")) or []
        enabled = cam.get("enabled", True) is not False
        # Prefer the detect-role input (lowest resolution Frigate analyses itself), then any input, if it is a restream path.
        ordered = sorted(inputs, key=lambda i: 0 if "detect" in (i.get("roles") or []) else 1)
        stream = next((s for s in (_restream_name(i.get("path", ""), rtsp_port) for i in ordered) if s), None)
        note = ""
        if stream is None and name in go2rtc_streams:
            stream = name
        if stream is None:
            note = "no go2rtc restream for this camera; add it under go2rtc.streams in Frigate"
        elif go2rtc_streams and stream not in go2rtc_streams and stream.split("?")[0] not in go2rtc_streams:
            note = f"stream '{stream}' is not defined in go2rtc.streams"
        out.append(FrigateCamera(name=str(name), stream=stream, enabled=enabled, note=note))
    return out


def parse_config_file(path: Path, rtsp_port: int = 8554) -> list[FrigateCamera]:
    try:
        return parse_config(yaml.safe_load(path.read_text()), rtsp_port)
    except (OSError, yaml.YAMLError) as e:
        raise FrigateError(f"cannot read {path}: {e}") from None


async def fetch_cameras(base_url: str, rtsp_port: int = 8554, token: str | None = None, http: httpx.AsyncClient | None = None) -> list[FrigateCamera]:
    own = http is None
    http = http or httpx.AsyncClient(timeout=httpx.Timeout(8.0))
    try:
        r = await http.get(base_url.rstrip("/") + "/api/config", headers={"authorization": f"Bearer {token}"} if token else {})
        if r.status_code != 200:
            raise FrigateError(f"Frigate answered HTTP {r.status_code} for /api/config (auth enabled? use the unauthenticated port 5000)")
        return parse_config(r.json(), rtsp_port)
    except httpx.HTTPError as e:
        raise FrigateError(f"cannot reach Frigate at {base_url}: {type(e).__name__}") from None
    finally:
        if own:
            await http.aclose()


def restream_url(frigate_url: str, stream: str, rtsp_port: int = 8554) -> str:
    host = urlparse(frigate_url).hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if not re.fullmatch(r"[A-Za-z0-9_.@:+-]+", stream):
        raise FrigateError(f"unexpected stream name: {stream!r}")
    return f"rtsp://{host}:{rtsp_port}/{stream}"
