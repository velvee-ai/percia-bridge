"""Optional anonymous product events. Closed allow-list: names and every property value must be known values."""

from __future__ import annotations

import platform
import re
from typing import Any

import httpx

EVENTS = {"bridge_started", "bridge_camera_streaming", "bridge_camera_failed", "bridge_camera_revoked"}
CLOSED: dict[str, set[str]] = {
    "source_type": {"onvif", "rtsp", "frigate", "homeassistant"},
    "mode": {"copy", "transcode"},
    "reason": {"source", "network", "auth", "ffmpeg", "stall", "other"},
    "arch": {"amd64", "arm64", "other"},
    "variant": {"docker", "ha_addon", "pip"},
    "camera_bucket": {"1", "2-5", "6-10"},
}
PATTERNS = {"version": re.compile(r"^\d+\.\d+\.\d+$")}


def sanitize(name: str, props: dict[str, Any]) -> tuple[str, dict[str, str]] | None:
    """Return the event as it may be sent, or None if the event name is unknown. Unknown properties/values are dropped."""
    if name not in EVENTS:
        return None
    clean: dict[str, str] = {}
    for k, v in props.items():
        if not isinstance(v, str):
            continue
        if k in CLOSED and v in CLOSED[k]:
            clean[k] = v
        elif k in PATTERNS and PATTERNS[k].match(v):
            clean[k] = v
    return name, clean


def bucket(n: int) -> str:
    return "1" if n <= 1 else "2-5" if n <= 5 else "6-10"


def arch() -> str:
    m = platform.machine().lower()
    return "amd64" if m in ("x86_64", "amd64") else "arm64" if m in ("aarch64", "arm64") else "other"


class Telemetry:
    def __init__(self, enabled: bool, key: str, install_id: str, variant: str, version: str, host: str = "https://eu.i.posthog.com"):
        self.enabled = bool(enabled and key and install_id)
        self.key, self.install_id, self.host = key, install_id, host.rstrip("/")
        self.base = {"arch": arch(), "variant": variant, "version": version}
        self._tasks: set = set()

    def emit(self, name: str, props: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        ev = sanitize(name, {**self.base, **(props or {})})
        if not ev:
            return
        import asyncio
        try:
            t = asyncio.get_running_loop().create_task(self._send(*ev))
        except RuntimeError:
            return
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _send(self, name: str, props: dict[str, str]) -> None:
        body = {"api_key": self.key, "event": name, "distinct_id": self.install_id,
                "properties": {**props, "$process_person_profile": False}}
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                await c.post(f"{self.host}/capture/", json=body)
        except Exception:  # noqa: BLE001 - telemetry must never matter
            pass
