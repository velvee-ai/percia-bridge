"""YAML config: loading, validation, ${ENV} expansion, safe writing."""

from __future__ import annotations

import difflib
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .logs import event, register_secret

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}$")
SOURCE_TYPES = ("rtsp", "onvif", "frigate", "homeassistant")
ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    pass


def default_config_path() -> Path:
    if os.environ.get("PERCIA_BRIDGE_CONFIG"):
        return Path(os.environ["PERCIA_BRIDGE_CONFIG"])
    if Path("/config").is_dir() and os.access("/config", os.W_OK):
        return Path("/config/config.yaml")
    return Path.home() / ".config" / "percia-bridge" / "config.yaml"


def default_state_dir(config_path: Path) -> Path:
    if os.environ.get("PERCIA_BRIDGE_STATE"):
        return Path(os.environ["PERCIA_BRIDGE_STATE"])
    if Path("/data").is_dir() and os.access("/data", os.W_OK):
        return Path("/data")
    return config_path.parent / "state"


@dataclass
class Video:
    mode: str = "auto"  # auto | copy | transcode
    max_height: int | None = None  # None = keep source size when copying; transcode default 720
    max_fps: int = 15
    max_bitrate_kbps: int = 1500
    preset: str = "veryfast"


@dataclass
class Source:
    type: str
    url: str = ""  # rtsp / frigate base url / resolved homeassistant stream
    host: str = ""
    port: int = 80
    username: str = ""
    password: str = ""
    profile: str = ""
    camera: str = ""  # frigate camera or HA entity id
    rtsp_port: int = 8554

    def describe(self) -> str:
        from .logs import strip_credentials

        if self.type == "rtsp":
            return strip_credentials(self.url)
        if self.type == "onvif":
            return f"onvif://{self.host}:{self.port}" + (f"#{self.profile}" if self.profile else "")
        if self.type == "frigate":
            return f"frigate:{self.camera}@{strip_credentials(self.url)}"
        return f"{self.type}:{self.camera}"


@dataclass
class CameraCfg:
    name: str
    source: Source
    video: Video
    external_id: str = ""  # optional override of the id used to register with Percia (see identity.py)


@dataclass
class Config:
    path: Path
    server: str = "https://percia.ai"
    api_key_file: Path | None = None
    state_dir: Path = Path("state")
    auto_add: bool = True
    health_listen: str = "127.0.0.1:9477"
    video: Video = field(default_factory=Video)
    rtsp_transport: str = "tcp"
    telemetry: bool = False
    telemetry_key: str = ""
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    cameras: list[CameraCfg] = field(default_factory=list)
    variant: str = "pip"  # docker | ha_addon | pip

    def camera(self, name: str) -> CameraCfg | None:
        return next((c for c in self.cameras if c.name == name), None)


TOP_KEYS = {"server", "api_key_file", "state_dir", "auto_add", "health", "video", "rtsp_transport", "telemetry",
            "telemetry_key", "ffmpeg_bin", "ffprobe_bin", "cameras", "variant"}
VIDEO_KEYS = {"mode", "max_height", "max_fps", "max_bitrate_kbps", "preset"}
SOURCE_KEYS = {"type", "url", "host", "port", "username", "password", "profile", "camera", "rtsp_port"}


def expand_env(value: Any, env: dict[str, str] | None = None) -> Any:
    env = os.environ if env is None else env
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            if m.group(1) not in env:
                raise ConfigError(f"environment variable {m.group(1)} is not set (used in the config as ${{{m.group(1)}}})")
            return env[m.group(1)]
        return ENV_RE.sub(sub, value)
    if isinstance(value, list):
        return [expand_env(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v, env) for k, v in value.items()}
    return value


def _unknown(where: str, got: set[str], allowed: set[str]) -> None:
    bad = sorted(got - allowed)
    if bad:
        hints = []
        for b in bad:
            close = difflib.get_close_matches(str(b), sorted(allowed), n=1)
            hints.append(f"{b} (did you mean {close[0]}?)" if close else str(b))
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(hints)}. Allowed: {', '.join(sorted(allowed))}")


def parse_video(raw: Any, base: Video | None = None, where: str = "video") -> Video:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    _unknown(where, set(raw), VIDEO_KEYS)
    v = Video(**vars(base)) if base else Video()
    for k, val in raw.items():
        setattr(v, k, val)
    if v.mode not in ("auto", "copy", "transcode"):
        raise ConfigError(f"{where}.mode must be auto, copy or transcode")
    for k in ("max_fps", "max_bitrate_kbps"):
        if not isinstance(getattr(v, k), int) or getattr(v, k) <= 0:
            raise ConfigError(f"{where}.{k} must be a positive integer")
    if v.max_height is not None and (not isinstance(v.max_height, int) or v.max_height < 120):
        raise ConfigError(f"{where}.max_height must be an integer >= 120")
    if not re.fullmatch(r"[a-z]+", str(v.preset)):
        raise ConfigError(f"{where}.preset is not a valid x264 preset")
    return v


def parse_source(raw: Any, where: str) -> Source:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping with a type")
    _unknown(where, set(raw), SOURCE_KEYS)
    t = raw.get("type")
    if t not in SOURCE_TYPES:
        raise ConfigError(f"{where}.type must be one of {', '.join(SOURCE_TYPES)}")
    s = Source(type=t, **{k: v for k, v in raw.items() if k != "type"})
    if t == "rtsp":
        if not re.match(r"^rtsps?://", s.url or ""):
            raise ConfigError(f"{where}.url must start with rtsp:// or rtsps://")
    elif t == "onvif":
        if not s.host:
            raise ConfigError(f"{where}.host is required for onvif")
    elif t == "frigate":
        if not s.url or not s.camera:
            raise ConfigError(f"{where}: frigate needs url (http://host:5000) and camera")
        if not re.match(r"^https?://", s.url):
            raise ConfigError(f"{where}.url must be http(s)://")
    elif t == "homeassistant":
        if not s.camera:
            raise ConfigError(f"{where}.camera (entity id) is required")
    for secret in (s.password, s.username):
        register_secret(secret)
    if t == "rtsp":
        m = re.match(r"^rtsps?://[^/@]*:([^/@]*)@", s.url)
        if m:
            register_secret(m.group(1))
    return s


def validate_server(server: str) -> str:
    u = urlparse(server)
    local = u.hostname in ("localhost", "127.0.0.1", "::1")
    if u.scheme != "https" and not (u.scheme == "http" and local):
        raise ConfigError("server must be an https:// URL (http is only allowed for localhost, for tests)")
    if not u.hostname:
        raise ConfigError("server has no host")
    return server.rstrip("/")


def parse(raw: Any, path: Path, env: dict[str, str] | None = None) -> Config:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("the config must be a YAML mapping")
    raw = expand_env(raw, env)
    _unknown("the config", set(raw), TOP_KEYS)
    cfg = Config(path=path)
    cfg.server = validate_server(raw.get("server", cfg.server))
    if raw.get("api_key_file"):
        cfg.api_key_file = Path(str(raw["api_key_file"])).expanduser()
    cfg.state_dir = Path(str(raw.get("state_dir") or default_state_dir(path))).expanduser()
    cfg.auto_add = bool(raw.get("auto_add", True))
    health = raw.get("health") or {}
    if not isinstance(health, dict):
        raise ConfigError("health must be a mapping")
    _unknown("health", set(health), {"listen"})
    cfg.health_listen = str(health.get("listen", cfg.health_listen))
    cfg.video = parse_video(raw.get("video"))
    if raw.get("rtsp_transport", "tcp") not in ("tcp", "udp"):
        raise ConfigError("rtsp_transport must be tcp or udp")
    cfg.rtsp_transport = raw.get("rtsp_transport", "tcp")
    cfg.telemetry = bool(raw.get("telemetry", False))
    cfg.telemetry_key = str(raw.get("telemetry_key", ""))
    cfg.ffmpeg_bin = str(raw.get("ffmpeg_bin", "ffmpeg"))
    cfg.ffprobe_bin = str(raw.get("ffprobe_bin", "ffprobe"))
    cfg.variant = str(raw.get("variant", "pip"))
    seen: set[str] = set()
    cams = raw.get("cameras") or []
    if not isinstance(cams, list):
        raise ConfigError("cameras must be a list")
    for i, c in enumerate(cams):
        where = f"cameras[{i}]"
        if not isinstance(c, dict):
            raise ConfigError(f"{where} must be a mapping")
        _unknown(where, set(c), {"name", "source", "video", "external_id"})
        name = str(c.get("name", ""))
        if not NAME_RE.match(name):
            raise ConfigError(f"{where}.name must be 1-60 characters: letters, digits, space, _ . -")
        if name.lower() in seen:
            raise ConfigError(f"duplicate camera name: {name}")
        seen.add(name.lower())
        ext = str(c.get("external_id") or "")
        if ext and not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", ext):
            raise ConfigError(f"{where}.external_id must be 1-100 characters of letters, digits . _ : -")
        cfg.cameras.append(CameraCfg(name, parse_source(c.get("source"), f"{where}.source"),
                                     parse_video(c.get("video"), cfg.video, f"{where}.video"), ext))
    if len(cfg.cameras) > 10:
        raise ConfigError("Percia allows at most 10 cameras per account")
    return cfg


def load(path: Path, env: dict[str, str] | None = None) -> Config:
    if not path.exists():
        raise ConfigError(f"config file not found: {path} (run `percia-bridge init`)")
    warn_if_open(path)
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from None
    return parse(raw, path, env)


def warn_if_open(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        event(logging.WARNING, "file_permissions_open", path=str(path), mode=oct(mode & 0o777), fix="chmod 600")


def write_private(path: Path, text: str) -> None:
    """Atomic write with mode 0600; parent directory 0700 when we create it."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_raw(path: Path) -> dict[str, Any]:
    """The file as written (no env expansion), for editing without baking in secrets from the environment."""
    return yaml.safe_load(path.read_text()) or {}


def save_raw(path: Path, raw: dict[str, Any]) -> None:
    write_private(path, yaml.safe_dump(raw, sort_keys=False, default_flow_style=False))
