"""The Bridge: owns the camera workers, creates/claims cameras when missing, and serves health + metrics."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable

from . import __version__
from .api import ApiError, FeatureOff, Forbidden, PerciaApi, Unauthorized, describe_exception
from .config import CameraCfg, Config
from .frigate import fetch_cameras, restream_url
from .httpd import json_response
from .identity import resolve_external_id
from .logs import event, redact
from .onvif import OnvifClient
from .state import CameraState, State
from .supervisor import REVOKED, STREAMING, Backoff, CameraWorker, ResolvedInput, Resolver, Timing


def make_resolver(cfg: Config, ha_resolve=None) -> Resolver:
    """Turn a camera's source into the URL ffmpeg should read, at every (re)start."""

    async def resolve(cam: CameraCfg) -> ResolvedInput:
        s = cam.source
        if s.type == "rtsp":
            return ResolvedInput(s.url)
        if s.type == "onvif":
            client = OnvifClient(s.host, s.port, s.username, s.password)
            try:
                return ResolvedInput(await client.stream_url(s.profile))
            finally:
                await client.aclose()
        if s.type == "frigate":
            stream = s.camera
            try:
                found = next((c for c in await fetch_cameras(s.url, s.rtsp_port) if c.name == s.camera), None)
                if found and found.stream:
                    stream = found.stream
            except Exception:  # noqa: BLE001 - Frigate API down: fall back to the conventional restream name
                pass
            return ResolvedInput(restream_url(s.url, stream, s.rtsp_port))
        if s.type == "homeassistant" and ha_resolve:
            url, headers = await ha_resolve(s)
            return ResolvedInput(url, headers)
        raise ValueError(f"unsupported source type {s.type}")

    return resolve


@dataclass
class Entry:
    cfg: CameraCfg
    task: asyncio.Task | None = None
    worker: CameraWorker | None = None
    setup_error: str = ""
    stopping: bool = False


class Bridge:
    def __init__(self, cfg: Config, api: PerciaApi, state: State, resolver: Resolver, api_key: Callable[[], str],
                 telemetry=None, timing: Timing | None = None, **worker_kwargs):
        self.cfg, self.api, self.state, self.resolver, self.api_key = cfg, api, state, resolver, api_key
        self.telemetry, self.timing, self.worker_kwargs = telemetry, timing, worker_kwargs
        self.entries: dict[str, Entry] = {}
        self.started = time.monotonic()

    def _emit(self, name: str, props: dict) -> None:
        if self.telemetry:
            self.telemetry.emit(name, props)

    async def apply(self, cams: list[CameraCfg]) -> None:
        """Reconcile running workers with the desired camera list (start new, stop removed, restart changed)."""
        want = {c.name: c for c in cams}
        for name in [n for n in self.entries if n not in want or self.entries[n].cfg != want[n]]:
            await self._stop_entry(self.entries.pop(name))
        for name, cam in want.items():
            if name not in self.entries:
                e = Entry(cam)
                e.task = asyncio.create_task(self._run_camera(e), name=f"camera:{name}")
                self.entries[name] = e

    async def _stop_entry(self, e: Entry) -> None:
        e.stopping = True
        if e.worker:
            await e.worker.stop()
        if e.task:
            e.task.cancel()
            await asyncio.gather(e.task, return_exceptions=True)

    async def _ensure_state(self, e: Entry) -> CameraState | None:
        name = e.cfg.name
        cs = self.state.get(name)
        if cs:
            return cs
        if not self.cfg.auto_add:
            e.setup_error = f"not added yet: run `percia-bridge add {name}`"
            return None
        backoff = Backoff(self.timing or Timing())
        ext = await resolve_external_id(e.cfg)
        while not e.stopping:
            try:
                reg = await self.api.register(name, self.api_key(), ext, None)
                self.state.put(name, reg.state)
                event(logging.INFO, "camera_added", camera=name, flow=reg.flow, existing=reg.existing, rotated=reg.rotated)
                e.setup_error = ""
                return reg.state
            except Unauthorized:
                e.setup_error = "Percia rejected the API key (401). Create a new key and put it in the key file or PERCIA_API_KEY."
            except (FeatureOff, Forbidden) as ex:
                e.setup_error = ex.message
            except ApiError as ex:
                e.setup_error = f"{ex.code}: {ex.message}" if ex.status else ex.message
                if ex.status >= 500:  # Percia hiccup: retry
                    await asyncio.sleep(backoff.next())
                    continue
            except Exception as ex:  # noqa: BLE001 - network: retry
                e.setup_error = describe_exception(ex)
                await asyncio.sleep(backoff.next())
                continue
            break
        event(logging.ERROR, "camera_setup_failed", camera=name, error=redact(e.setup_error))
        return None

    async def _run_camera(self, e: Entry) -> None:
        cs = await self._ensure_state(e)
        while cs and not e.stopping:
            e.worker = CameraWorker(self.cfg, e.cfg, cs, self.api, self.resolver, self.timing, on_event=self._emit, **self.worker_kwargs)
            await e.worker.run()
            if e.stopping or e.worker.rt.state != REVOKED:
                return
            # Token rejected. Wait for `percia-bridge rotate` (another process) to write a new one, then carry on.
            old = cs.token
            cs = None
            while not e.stopping:
                await asyncio.sleep((self.timing or Timing()).revoked_poll)
                try:
                    self.state.load()
                except Exception:  # noqa: BLE001 - a half-written file is retried on the next tick
                    continue
                fresh = self.state.get(e.cfg.name)
                if fresh and fresh.token != old:
                    event(logging.INFO, "camera_token_updated", camera=e.cfg.name)
                    cs = fresh
                    break

    async def shutdown(self) -> None:
        await asyncio.gather(*(self._stop_entry(e) for e in self.entries.values()), return_exceptions=True)
        self.entries.clear()

    # ---- observability ----
    def cameras(self) -> list[dict]:
        out = []
        for name, e in self.entries.items():
            if e.worker:
                out.append(e.worker.snapshot())
            else:
                out.append({"name": name, "source": e.cfg.source.type, "state": "setup_failed" if e.setup_error else "setup", "mode": "",
                            "uptime_s": 0, "restarts": 0, "last_error": e.setup_error, "reason": "auth" if e.setup_error else "", "fps": 0,
                            "speed": 0, "heartbeat_failures": 0})
        return out

    def health(self) -> dict:
        cams = self.cameras()
        streaming = sum(1 for c in cams if c["state"] == STREAMING)
        status = "ok" if not cams or streaming == len(cams) else "down" if streaming == 0 else "degraded"
        return {"status": status, "version": __version__, "uptime_s": round(time.monotonic() - self.started), "cameras": cams}

    def metrics(self) -> str:
        cams = self.cameras()
        lines = ["# TYPE percia_bridge_up gauge", "percia_bridge_up 1",
                 "# TYPE percia_bridge_camera_up gauge", "# TYPE percia_bridge_camera_restarts_total counter",
                 "# TYPE percia_bridge_camera_fps gauge", "# TYPE percia_bridge_camera_streaming_seconds gauge",
                 "# TYPE percia_bridge_heartbeat_failures_total counter", "# TYPE percia_bridge_ffmpeg_speed gauge"]
        esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
        for c in cams:
            lb = f'{{camera="{esc(c["name"])}"}}'
            lines += [f'percia_bridge_camera_up{lb} {1 if c["state"] == STREAMING else 0}',
                      f'percia_bridge_camera_restarts_total{lb} {c["restarts"]}', f'percia_bridge_camera_fps{lb} {c["fps"]}',
                      f'percia_bridge_camera_streaming_seconds{lb} {c["uptime_s"]}',
                      f'percia_bridge_heartbeat_failures_total{lb} {c["heartbeat_failures"]}',
                      f'percia_bridge_ffmpeg_speed{lb} {c["speed"]}']
        return "\n".join(lines) + "\n"

    async def http(self, method: str, path: str, headers: dict, body: bytes, peer: tuple):
        if method != "GET":
            return 405, "text/plain", b"method not allowed"
        if path == "/healthz":
            h = self.health()
            return json_response(503 if h["status"] == "down" else 200, h)
        if path == "/metrics":
            return 200, "text/plain; version=0.0.4", self.metrics().encode()
        return 404, "text/plain", b"not found"
