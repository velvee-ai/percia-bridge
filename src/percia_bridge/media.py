"""ffprobe + ffmpeg command construction. Pure functions where possible so they are unit-testable."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

from .config import Video

DEFAULT_MAX_HEIGHT = 720


@dataclass
class Probe:
    codec: str = ""
    profile: str = ""
    width: int = 0
    height: int = 0
    has_b_frames: int = 0


@dataclass
class Plan:
    mode: str  # copy | transcode
    reason: str


def parse_probe(text: str) -> Probe:
    data = json.loads(text)
    streams = data.get("streams") or []
    if not streams:
        raise ValueError("no video stream found")
    s = streams[0]
    return Probe(codec=s.get("codec_name", ""), profile=s.get("profile", ""), width=int(s.get("width") or 0),
                 height=int(s.get("height") or 0), has_b_frames=int(s.get("has_b_frames") or 0))


def plan(probe: Probe | None, video: Video) -> Plan:
    """Pick the cheapest path Percia can decode. `probe=None` (could not probe) means transcode in auto mode."""
    if video.mode == "copy":
        return Plan("copy", "forced")
    if video.mode == "transcode":
        return Plan("transcode", "forced")
    if probe is None:
        return Plan("transcode", "probe_failed")
    if probe.codec != "h264":
        return Plan("transcode", f"codec_{probe.codec or 'unknown'}")
    if probe.has_b_frames > 0:
        return Plan("transcode", "b_frames")
    if video.max_height and probe.height > video.max_height:
        return Plan("transcode", "too_tall")
    return Plan("copy", "h264")


def input_args(url: str, rtsp_transport: str, extra_headers: dict[str, str] | None = None) -> list[str]:
    args = ["-fflags", "+genpts+discardcorrupt"]
    if re.match(r"(?i)^rtsps?://", url):
        args += ["-rtsp_transport", rtsp_transport, "-timeout", "15000000"]
    else:
        args += ["-rw_timeout", "15000000", "-use_wallclock_as_timestamps", "1"]
        if extra_headers:
            args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in extra_headers.items())]
    return args + ["-i", url]


def video_args(plan_: Plan, video: Video) -> list[str]:
    if plan_.mode == "copy":
        return ["-c:v", "copy"]
    height = video.max_height or DEFAULT_MAX_HEIGHT
    b = video.max_bitrate_kbps
    return [
        "-vf", f"scale=-2:'min(ih,{height})',fps={video.max_fps},format=yuv420p",
        "-c:v", "libx264", "-profile:v", "baseline", "-preset", video.preset, "-tune", "zerolatency",
        "-bf", "0", "-g", str(video.max_fps * 2), "-maxrate", f"{b}k", "-bufsize", f"{2 * b}k",
    ]


def build_command(ffmpeg: str, url: str, whip_url: str, token: str, plan_: Plan, video: Video, rtsp_transport: str = "tcp",
                  headers: dict[str, str] | None = None) -> list[str]:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-progress", "pipe:1", "-stats_period", "2"]
    cmd += input_args(url, rtsp_transport, headers)
    cmd += ["-map", "0:v:0", "-an"] + video_args(plan_, video)
    cmd += ["-f", "whip", "-authorization", token, whip_url]
    if plan_.mode == "transcode":
        cmd = ["nice", "-n", "10"] + cmd
    return cmd


def probe_command(ffprobe: str, url: str, rtsp_transport: str) -> list[str]:
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=codec_name,profile,width,height,has_b_frames", "-of", "json"]
    if re.match(r"(?i)^rtsps?://", url):
        cmd += ["-rtsp_transport", rtsp_transport, "-timeout", "10000000"]
    return cmd + [url]


async def run_probe(ffprobe: str, url: str, rtsp_transport: str, timeout: float = 15.0) -> Probe | None:
    try:
        proc = await asyncio.create_subprocess_exec(*probe_command(ffprobe, url, rtsp_transport),
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        if proc.returncode != 0:
            return None
        return parse_probe(out.decode())
    except (asyncio.TimeoutError, ValueError, OSError):
        try:
            proc.kill()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return None


def parse_progress_line(line: str) -> tuple[str, str] | None:
    if "=" not in line:
        return None
    k, _, v = line.strip().partition("=")
    return k, v


_AUTH_RE = re.compile(r"(?i)\b(401|403)\b|unauthori[sz]ed|forbidden")
_SOURCE_CTX_RE = re.compile(r"(?i)\[in#|opening input|\[rtsps?\b|DESCRIBE|SETUP|OPTIONS|rtsp://")
_PERCIA_CTX_RE = re.compile(r"(?i)\[out#|whip|could not write header")


def classify_error(line: str) -> str | None:
    """'percia_auth' when the WHIP (output) side answered 401/403 (camera token removed or re-paired); 'source_auth' when
    the camera/input refused our credentials; None for anything else. Real ffmpeg 8 lines:
      [out#0/whip @ ..] Could not write header (incorrect codec parameters ?): Server returned 401 Unauthorized (authorization failed)
      [in#0 @ ..] Error opening input: Server returned 401 Unauthorized (authorization failed)"""
    if not _AUTH_RE.search(line):
        return None
    if _PERCIA_CTX_RE.search(line):
        return "percia_auth"
    if _SOURCE_CTX_RE.search(line):
        return "source_auth"
    return None
