"""One worker per camera: ffmpeg RTSP -> WHIP with reconnects, heartbeats and a 401 stop. Pure asyncio."""

from __future__ import annotations

import asyncio
import logging
import random
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import media
from .api import PerciaApi, Unauthorized
from .config import CameraCfg, Config
from .logs import event, redact
from .state import CameraState

STARTING, STREAMING, BACKOFF, REVOKED, STOPPED = "starting", "streaming", "backoff", "revoked", "stopped"


@dataclass
class Timing:
    heartbeat_interval: float = 20.0
    stable_after: float = 60.0  # a session this long resets the backoff
    stall_after: float = 30.0  # streaming but no new frames
    startup_timeout: float = 45.0  # no first frame yet
    backoff_base: float = 1.0
    backoff_cap: float = 30.0
    jitter: float = 0.2
    stop_grace: float = 5.0
    revoked_poll: float = 5.0  # how often a revoked camera looks for a rotated token in the state dir


class Backoff:
    """1 s, 2 s, 4 s ... capped at 30 s, with +/- jitter."""

    def __init__(self, timing: Timing, rng: random.Random | None = None):
        self.t, self.n, self.rng = timing, 0, rng or random.Random()

    def next(self) -> float:
        base = min(self.t.backoff_cap, self.t.backoff_base * (2 ** self.n))
        self.n += 1
        if self.t.jitter:
            base *= 1 + self.rng.uniform(-self.t.jitter, self.t.jitter)
        return base

    def reset(self) -> None:
        self.n = 0


@dataclass
class ResolvedInput:
    url: str
    headers: dict[str, str] = field(default_factory=dict)


Resolver = Callable[[CameraCfg], Awaitable[ResolvedInput]]


def revoked_hint(name: str, kind: str) -> str:
    if kind == "push":
        return (f"Percia rejected the camera token (it was rotated or revoked, or the camera was removed). "
                f"Get a new one with: percia-bridge rotate \"{name}\"  (a running bridge picks it up within seconds)")
    return f"Percia rejected the camera token (removed or re-paired). Run: percia-bridge add \"{name}\" --force"


class SourceError(Exception):
    pass


@dataclass
class Runtime:
    state: str = STARTING
    mode: str = ""
    plan_reason: str = ""
    reason: str = ""  # why not streaming: source | network | auth | ffmpeg | stall | other
    last_error: str = ""
    restarts: int = 0
    heartbeat_failures: int = 0
    fps: float = 0.0
    speed: float = 0.0
    frames: int = 0
    streaming_since: float | None = None
    started_at: float = field(default_factory=time.monotonic)

    def uptime(self) -> float:
        return time.monotonic() - self.streaming_since if self.streaming_since else 0.0


class CameraWorker:
    def __init__(self, cfg: Config, cam: CameraCfg, cstate: CameraState, api: PerciaApi, resolver: Resolver,
                 timing: Timing | None = None, command_builder=None, prober=None, on_event: Callable[[str, dict], None] | None = None):
        self.cfg, self.cam, self.cstate, self.api, self.resolver = cfg, cam, cstate, api, resolver
        self.timing = timing or Timing()
        self.rt = Runtime()
        self._stop = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._backoff = Backoff(self.timing)
        self._build = command_builder or media.build_command
        self._probe = prober or media.run_probe
        self._on_event = on_event or (lambda name, props: None)
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._revoked = False
        self._frame_at = time.monotonic()

    # ---- public ----
    async def run(self) -> None:
        name = self.cam.name
        while not self._stop.is_set():
            self.rt.state, self.rt.fps, self.rt.frames = STARTING, 0.0, 0
            outcome = await self._session()
            if outcome == REVOKED:
                self.rt.state, self.rt.reason = REVOKED, "auth"
                hint = revoked_hint(name, self.cstate.kind)
                self.rt.last_error = hint
                event(logging.ERROR, "camera_revoked", camera=name, hint=hint)
                self._on_event("bridge_camera_revoked", {"source_type": self.cam.source.type})
                return
            if self._stop.is_set():
                break
            if self.rt.streaming_since and time.monotonic() - self.rt.streaming_since >= self.timing.stable_after:
                self._backoff.reset()
            self.rt.streaming_since = None
            delay = self._backoff.next()
            self.rt.state, self.rt.restarts = BACKOFF, self.rt.restarts + 1
            event(logging.WARNING, "camera_backoff", camera=name, reason=self.rt.reason, retry_in_s=round(delay, 1),
                  error=self.rt.last_error)
            self._on_event("bridge_camera_failed", {"source_type": self.cam.source.type, "reason": self.rt.reason or "other"})
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
        self.rt.state = STOPPED if not self._revoked else REVOKED

    async def stop(self) -> None:
        self._stop.set()
        await self._terminate()

    # ---- one ffmpeg session ----
    async def _session(self) -> str:
        cam = self.cam
        try:
            inp = await self.resolver(cam)
        except Exception as e:  # noqa: BLE001 - any resolver failure is a source problem
            self.rt.reason, self.rt.last_error = "source", redact(f"cannot resolve the camera stream: {e}")
            return "failed"
        if self._stop.is_set():
            return "stopped"
        probe = await self._probe(self.cfg.ffprobe_bin, inp.url, self.cfg.rtsp_transport)
        plan = media.plan(probe, cam.video)
        self.rt.mode, self.rt.plan_reason = plan.mode, plan.reason
        cmd = self._build(self.cfg.ffmpeg_bin, inp.url, self.cstate.whip_url, self.cstate.token, plan, cam.video,
                          self.cfg.rtsp_transport, inp.headers)
        self._stderr_tail.clear()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        except OSError as e:
            self.rt.reason, self.rt.last_error = "ffmpeg", redact(f"cannot start ffmpeg: {e}")
            return "failed"
        event(logging.INFO, "ffmpeg_started", camera=cam.name, mode=plan.mode, why=plan.reason, source=cam.source.describe())
        proc = self._proc
        tasks = [asyncio.create_task(self._read_progress(proc)), asyncio.create_task(self._read_stderr(proc)),
                 asyncio.create_task(self._watchdog(proc)), asyncio.create_task(self._heartbeats(proc))]
        try:
            await proc.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        code = proc.returncode
        self._proc = None
        event(logging.INFO, "ffmpeg_exit", camera=cam.name, code=code)
        if self._revoked:
            return REVOKED
        if self._stop.is_set():
            return "stopped"
        if not self.rt.reason or self.rt.state == STREAMING:
            self.rt.reason = "network" if self.rt.streaming_since else "source"
        if self.rt.reason == "auth":  # the camera (not Percia) refused our credentials
            self.rt.reason = "source"
        if not self.rt.last_error:
            self.rt.last_error = redact(" | ".join(list(self._stderr_tail)[-3:]) or f"ffmpeg exited with code {code}")
        return "failed"

    async def _read_progress(self, proc) -> None:
        assert proc.stdout
        block: dict[str, str] = {}
        async for raw in proc.stdout:
            kv = media.parse_progress_line(raw.decode("utf-8", "replace"))
            if not kv:
                continue
            block[kv[0]] = kv[1]
            if kv[0] != "progress":
                continue
            try:
                frames = int(block.get("frame", "0") or 0)
            except ValueError:
                frames = 0
            if frames > self.rt.frames:
                self._frame_at = time.monotonic()
                if self.rt.state != STREAMING:
                    self.rt.state, self.rt.reason, self.rt.last_error = STREAMING, "", ""
                    self.rt.streaming_since = time.monotonic()
                    event(logging.INFO, "camera_streaming", camera=self.cam.name, mode=self.rt.mode)
                    self._on_event("bridge_camera_streaming", {"source_type": self.cam.source.type, "mode": self.rt.mode})
            self.rt.frames = frames
            for key, attr in (("fps", "fps"), ("speed", "speed")):
                try:
                    setattr(self.rt, attr, float(block.get(key, "0").rstrip("x") or 0))
                except ValueError:
                    pass
            block = {}

    async def _read_stderr(self, proc) -> None:
        assert proc.stderr
        async for raw in proc.stderr:
            line = redact(raw.decode("utf-8", "replace").strip())
            if not line:
                continue
            self._stderr_tail.append(line[:300])
            kind = media.classify_error(line)
            if kind == "percia_auth":
                self._revoked = True
                self.rt.reason = "auth"
                await self._terminate()
                return
            if kind == "source_auth":
                self.rt.reason = "auth"
                self.rt.last_error = "the camera rejected the username/password (RTSP 401)"

    async def _watchdog(self, proc) -> None:
        self._frame_at = time.monotonic()
        start = time.monotonic()
        while True:
            await asyncio.sleep(min(1.0, self.timing.stall_after / 4))
            now = time.monotonic()
            if self.rt.state == STREAMING and now - self._frame_at > self.timing.stall_after:
                self.rt.reason, self.rt.last_error = "stall", f"no new frames for {self.timing.stall_after:.0f}s"
                await self._terminate()
                return
            if self.rt.state != STREAMING and now - start > self.timing.startup_timeout:
                self.rt.reason = self.rt.reason or "source"
                self.rt.last_error = self.rt.last_error or f"no video after {self.timing.startup_timeout:.0f}s"
                await self._terminate()
                return

    async def _heartbeats(self, proc) -> None:
        failing = False
        while True:
            if self.rt.state == STREAMING:
                try:
                    await self.api.heartbeat(self.cstate.token)
                    failing = False
                except Unauthorized:
                    self._revoked = True
                    self.rt.reason = "auth"
                    await self._terminate()
                    return
                except Exception as e:  # noqa: BLE001 - never stop video over a missed heartbeat
                    self.rt.heartbeat_failures += 1
                    if not failing:
                        event(logging.WARNING, "heartbeat_failed", camera=self.cam.name, error=redact(str(e)))
                    failing = True
            await asyncio.sleep(self.timing.heartbeat_interval if self.rt.state == STREAMING else 0.2)

    async def _terminate(self) -> None:
        proc = self._proc
        if not proc or proc.returncode is not None:
            return
        try:
            if proc.stdin:
                proc.stdin.write(b"q\n")
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), self.timing.stop_grace)
            return
        except asyncio.TimeoutError:
            pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), 2)
                return
            except asyncio.TimeoutError:
                continue

    def snapshot(self) -> dict:
        r = self.rt
        return {"name": self.cam.name, "source": self.cam.source.type, "state": r.state, "mode": r.mode,
                "uptime_s": round(r.uptime()), "restarts": r.restarts, "last_error": r.last_error, "reason": r.reason,
                "fps": r.fps, "speed": r.speed, "heartbeat_failures": r.heartbeat_failures}
