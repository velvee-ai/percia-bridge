"""Backoff, 401 stop, stall and recovery logic of the camera worker, with a fake ffmpeg and a real local fake Percia server."""
import asyncio
import random
import sys
import time
from pathlib import Path

from fakes import TOKEN

from percia_bridge import supervisor as sv
from percia_bridge.api import PerciaApi
from percia_bridge.config import CameraCfg, Config, Source, Video
from percia_bridge.state import CameraState

FAKE = str(Path(__file__).parent / "fake_ffmpeg.py")
FAST = sv.Timing(heartbeat_interval=0.1, stable_after=0.5, stall_after=0.6, startup_timeout=1.5, backoff_base=0.05, backoff_cap=0.2, jitter=0.0, stop_grace=2)


def run(coro):
    return asyncio.run(coro)


def test_backoff_doubles_and_caps_at_30_seconds():
    b = sv.Backoff(sv.Timing(jitter=0.0))
    assert [b.next() for _ in range(8)] == [1, 2, 4, 8, 16, 30, 30, 30]
    b.reset()
    assert b.next() == 1


def test_backoff_jitter_stays_within_20_percent():
    b = sv.Backoff(sv.Timing(), random.Random(1))
    for _ in range(200):
        b.n = 3
        assert 6.4 <= b.next() <= 9.6


def make_worker(percia, scenarios, timing=FAST, events=None):
    """scenarios: list of fake-ffmpeg scenario names, one per ffmpeg start (the last repeats)."""
    runs = []

    def builder(ffmpeg, url, whip, token, plan, video, transport, headers):
        sc = scenarios[min(len(runs), len(scenarios) - 1)]
        runs.append((sc, token))
        return [sys.executable, FAKE, sc]

    async def resolver(cam):
        return sv.ResolvedInput("rtsp://fake/s")

    async def prober(*a):
        return None

    cam = CameraCfg("cam", Source("rtsp", url="rtsp://fake/s"), Video())
    api = PerciaApi(percia.url)
    w = sv.CameraWorker(Config(path=Path("c")), cam, CameraState("cam_1", TOKEN, percia.url + "/w"), api, resolver, timing,
                        command_builder=builder, prober=prober, on_event=(lambda n, p: events.append((n, p))) if events is not None else None)
    return w, runs, api


async def until(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


def test_streams_and_heartbeats_with_camera_token(percia):
    async def go():
        events = []
        w, runs, api = make_worker(percia, ["stream"], events=events)
        t = asyncio.create_task(w.run())
        assert await until(lambda: w.rt.state == sv.STREAMING and percia.heartbeats >= 2)
        assert percia.bad_heartbeats == 0
        await w.stop()
        await t
        await api.aclose()
        assert w.rt.state == sv.STOPPED
        assert ("bridge_camera_streaming", {"source_type": "rtsp", "mode": ""}) in events or any(n == "bridge_camera_streaming" for n, _ in events)
    run(go())


def test_ffmpeg_exit_triggers_backoff_and_restart(percia):
    async def go():
        w, runs, api = make_worker(percia, ["die", "die", "stream"])
        t = asyncio.create_task(w.run())
        assert await until(lambda: w.rt.state == sv.STREAMING)
        assert len(runs) == 3 and w.rt.restarts == 2
        await w.stop()
        await t
        await api.aclose()
    run(go())


def test_backoff_delays_grow_between_restarts(percia):
    async def go():
        stamps = []
        w, runs, api = make_worker(percia, ["die"])
        orig = w._build

        def builder(*a):
            stamps.append(time.monotonic())
            return orig(*a)
        w._build = builder
        t = asyncio.create_task(w.run())
        assert await until(lambda: len(stamps) >= 5)
        await w.stop()
        await t
        await api.aclose()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert gaps[2] > gaps[0] + 0.05 and gaps[3] >= 0.19  # 0.05, 0.1, 0.2, 0.2 (cap) plus ~40 ms process start
    run(go())


def test_heartbeat_401_stops_this_camera_and_does_not_retry(percia):
    async def go():
        w, runs, api = make_worker(percia, ["stream"])
        t = asyncio.create_task(w.run())
        assert await until(lambda: w.rt.state == sv.STREAMING and percia.heartbeats >= 1)
        percia.revoked = True
        await asyncio.wait_for(t, 8)  # run() returns by itself: no more retries
        assert w.rt.state == sv.REVOKED and w.rt.reason == "auth"
        n = len(runs)
        await asyncio.sleep(0.5)
        assert len(runs) == n
        await api.aclose()
    run(go())


def test_whip_401_in_ffmpeg_output_stops_the_camera(percia):
    async def go():
        w, runs, api = make_worker(percia, ["whip401"])
        await asyncio.wait_for(w.run(), 8)
        assert w.rt.state == sv.REVOKED and len(runs) == 1
        assert percia.heartbeats == 0  # never reported online
        await api.aclose()
    run(go())


def test_camera_rtsp_401_is_a_source_problem_and_keeps_retrying(percia):
    async def go():
        w, runs, api = make_worker(percia, ["rtsp401"])
        t = asyncio.create_task(w.run())
        assert await until(lambda: len(runs) >= 3)
        assert w.rt.state in (sv.BACKOFF, sv.STARTING) and w.rt.reason == "source"
        assert "username/password" in w.rt.last_error
        await w.stop()
        await t
        await api.aclose()
    run(go())


def test_stall_is_detected_and_restarted(percia):
    async def go():
        w, runs, api = make_worker(percia, ["stall", "stream"])
        t = asyncio.create_task(w.run())
        assert await until(lambda: len(runs) >= 2 and w.rt.state == sv.STREAMING, 12)
        await w.stop()
        await t
        await api.aclose()
    run(go())


def test_no_first_frame_times_out_as_source_problem(percia):
    async def go():
        w, runs, api = make_worker(percia, ["nothing"], timing=sv.Timing(**{**FAST.__dict__, "startup_timeout": 0.5}))
        t = asyncio.create_task(w.run())
        assert await until(lambda: w.rt.state == sv.BACKOFF)
        assert w.rt.reason == "source" and "no video" in w.rt.last_error
        assert percia.heartbeats == 0  # not streaming -> no heartbeat -> Percia shows it offline
        await w.stop()
        await t
        await api.aclose()
    run(go())


def test_long_session_resets_backoff(percia):
    async def go():
        w, runs, api = make_worker(percia, ["die", "die", "die", "stream_then_die", "die"], timing=sv.Timing(**{**FAST.__dict__, "stable_after": 0.3}))
        t = asyncio.create_task(w.run())
        assert await until(lambda: len(runs) >= 5)
        assert w._backoff.n <= 2  # reset after the session that streamed >= stable_after
        await w.stop()
        await t
        await api.aclose()
    run(go())


def test_resolver_failure_is_backoff_source(percia):
    async def go():
        w, runs, api = make_worker(percia, ["stream"])

        async def bad(cam):
            raise RuntimeError("camera at rtsp://u:pw@10.0.0.9 not found")
        w.resolver = bad
        t = asyncio.create_task(w.run())
        assert await until(lambda: w.rt.state == sv.BACKOFF)
        assert w.rt.reason == "source" and "pw@" not in w.rt.last_error and not runs
        await w.stop()
        await t
        await api.aclose()
    run(go())


def test_heartbeat_network_failure_does_not_stop_video(percia):
    async def go():
        w, runs, api = make_worker(percia, ["stream"])
        t = asyncio.create_task(w.run())
        assert await until(lambda: w.rt.state == sv.STREAMING and percia.heartbeats >= 1)
        percia.close()  # Percia unreachable
        assert await until(lambda: w.rt.heartbeat_failures >= 2)
        assert w.rt.state == sv.STREAMING
        await w.stop()
        await t
        await api.aclose()
    run(go())
